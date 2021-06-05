import asyncio
import functools
import itertools
import math
import random

import discord
import json
import youtube_dl
from discord import utils
from discord import Activity, ActivityType
from async_timeout import timeout
from discord.ext import commands
from discord.ext.commands import Bot
from discord import utils
import nekos
import datetime
import time
# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} дней'.format(days))
        if hours > 0:
            duration.append('{} часов'.format(hours))
        if minutes > 0:
            duration.append('{} минут'.format(minutes))
        if seconds > 0:
            duration.append('{} секунд'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Опа,смотри что играет',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Время прослушивания', value=self.source.duration)
                 .add_field(name='Запросил', value=self.requester.mention)
                 .add_field(name='Автор видео', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='Ссылка(кликни)', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('Эта команда не используется в ЛС (Личные сообщения)')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('Меня это пугает. Произошла какая-то ошибка: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            raise VoiceError('Вы не подключены к голосовому каналу. И не указали куда подключаться.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            return await ctx.send('Бот и так не подключен. Зачем его кикать?')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the player."""

        if not ctx.voice_state.is_playing:
            return await ctx.send('Сейчас музыка не играет. Можете включить.')

        if 0 > volume > 100:
            return await ctx.send('Volume must be between 0 and 100')

        ctx.voice_state.volume = volume / 100
        await ctx.send('Громкость изменена на {}%'.format(volume))

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if not ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Сейчас музыка не играет,зачем её пропускать? Можете включить.')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Голосование за пропуск добавлено. Проголосовали: **{}/3**'.format(total_votes))

        else:
            await ctx.send('Вы уже голосовали за пропуск этого трека.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.
        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('В очереди нет треков. Можете добавить.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('В очереди нет треков. Можете добавить.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('В очереди нет треков. Можете добавить.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Ничего не играет в данный момент.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song.
        If there are songs in the queue, this will be queued until the
        other songs finished playing.
        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('Произошла ошибка при обработке этого запроса: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('Успешно добавлено {}'.format(str(source)))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('Сначала подключись к голосовому.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Бот уже подключен с голосовому каналу.')


bot = commands.Bot('!', intents = discord.Intents().all())
bot.remove_command('help')
bot.add_cog(Music(bot))

@bot.command()
async def say(ctx, *, msg: str = None):
  await ctx.send(embed = discord.Embed(description = msg))
    
#Clear Chat
@bot.command( pass_context = True )

async def clear( ctx, amount = 100 ):
    await ctx.channel.purge( limit = amount )

@bot.event
async def on_member_join( member ):
    channel = bot.get_channel( 850100716559400960 )

    role = discord.utils.get( member.guild.roles, id = 850027060864352266 )

    await member.add_roles( role )
    await channel.send( embed = discord.Embed(description = f'Пользователь ``{ member.name }``, присоединился к нам!',
     color = 0x0c0c0c ) )

@bot.command( pass_context = True )

async def help( ctx ):
    emb = discord.Embed( title = 'Навигация по командам' )

    emb.add_field( name = '{}play'.format( '!' ), value = 'Включить музыку' )
    emb.add_field( name = '{}stop'.format( '!' ), value = 'Выключить музыку' )
    emb.add_field( name = '{}skip'.format( '!' ), value = 'Пропустить музыку' )
    emb.add_field( name = '{}cum'.format( '!' ), value = 'Что-то интересное в 18+ канале' )
    emb.add_field( name = '{}info'.format( '!' ), value = 'Узнать информацию о пользователе' )

    await ctx.send( embed = emb )

@bot.event
async def on_voice_state_update(member,before,after):
    if after.channel.id == 850728985854738440:
        for guild in bot.guilds:
            maincategory = discord.utils.get(guild.categories, id=850021698472247346)
            channel2 = await guild.create_voice_channel(name=f'канал {member.display_name}',category = maincategory)
            await channel2.set_permissions(member,connect=True,mute_members=True,move_members=True,manage_channels=True)
            await member.move_to(channel2)
            def check(x,y,z):
                return len(channel2.members) == 0
            await bot.wait_for('voice_state_update',check=check)
            await channel2.delete()

@bot.command()
async def timely(ctx):
    with open('economy.json','r') as f:
        money = json.load(f)
    if not str(ctx.author.id) in money:
        money[str(ctx.author.id)] = {}
        money[str(ctx.author.id)]['Money'] = 0

    if not str(ctx.author.id) in queue:
        emb = discord.Embed(description=f'**{ctx.author}** Вы получили свои 1250 монет')
        await ctx.send(embed= emb)
        money[str(ctx.author.id)]['Money'] += 1250
        queue.append(str(ctx.author.id))
        with open('economy.json','w') as f:
            json.dump(money,f)
        await asyncio.sleep(12*60)
        queue.remove(str(ctx.author.id))
    if str(ctx.author.id) in queue:
        emb = discord.Embed(description=f'**{ctx.author}** Вы уже получили свою награду')
        await ctx.send(embed= emb)
@bot.command()
async def balance(ctx,member:discord.Member = None):
    if member == ctx.author or member == None:
        with open('economy.json','r') as f:
            money = json.load(f)
        emb = discord.Embed(description=f'У **{ctx.author}** {money[str(ctx.author.id)]["Money"]} монет')
        await ctx.send(embed= emb)
    else:
        with open('economy.json','r') as f:
            money = json.load(f)
        emb = discord.Embed(description=f'У **{member}** {money[str(member.id)]["Money"]} монет')
        await ctx.send(embed= emb)
@bot.command()
async def addshop(ctx,role:discord.Role,cost:int):
    with open('economy.json','r') as f:
        money = json.load(f)
    if str(role.id) in money['shop']:
        await ctx.send("Эта роль уже есть в магазине")
    if not str(role.id) in money['shop']:
        money['shop'][str(role.id)] ={}
        money['shop'][str(role.id)]['Cost'] = cost
        await ctx.send('Роль добавлена в магазин')
    with open('economy.json','w') as f:
        json.dump(money,f)
@bot.command()
async def shop(ctx):
    with open('economy.json','r') as f:
        money = json.load(f)
    emb = discord.Embed(title="Магазин")
    for role in money['shop']:
        emb.add_field(name=f'Цена: {money["shop"][role]["Cost"]}',value=f'<@&{role}>',inline=False)
    await ctx.send(embed=emb)
@bot.command()
async def removeshop(ctx,role:discord.Role):
    with open('economy.json','r') as f:
        money = json.load(f)
    if not str(role.id) in money['shop']:
        await ctx.send("Этой роли нет в магазине")
    if str(role.id) in money['shop']:
        await ctx.send('Роль удалена из магазина')
        del money['shop'][str(role.id)]
    with open('economy.json','w') as f:
        json.dump(money,f)
@bot.command()
async def buy(ctx,role:discord.Role):
    with open('economy.json','r') as f:
        money = json.load(f)
    if str(role.id) in money['shop']:
        if money['shop'][str(role.id)]['Cost'] <= money[str(ctx.author.id)]['Money']:
            if not role in ctx.author.roles:
                await ctx.send('Вы купили роль!')
                for i in money['shop']:
                    if i == str(role.id):
                        buy = discord.utils.get(ctx.guild.roles,id = int(i))
                        await ctx.author.add_roles(buy)
                        money[str(ctx.author.id)]['Money'] -= money['shop'][str(role.id)]['Cost']
            else:
                await ctx.send('У вас уже есть эта роль!')
    with open('economy.json','w') as f:
        json.dump(money,f)

@bot.command()
async def give(ctx,member:discord.Member,arg:int):
    with open('economy.json','r') as f:
        money = json.load(f)
    if money[str(ctx.author.id)]['Money'] >= arg:
        emb = discord.Embed(description=f'**{ctx.author}** подарил **{member}** **{arg}** монет')
        money[str(ctx.author.id)]['Money'] -= arg
        money[str(member.id)]['Money'] += arg
        await ctx.send(embed = emb)
    else:
        await ctx.send('У вас недостаточно денег')
    with open('economy.json','w') as f:
        json.dump(money,f)

@bot.event
async def on_raw_reaction_add(payload):
    guild = await bot.fetch_guild(787791032745590845)
    if payload.message_id == 850785149519527976 and payload.emoji.name == "💜":
        role = discord.utils.get(guild.roles, id = 793859329672347679)
        await payload.member.add_roles(role)

@bot.command()
@commands.has_permissions(view_audit_log=True)
async def mute(ctx,member:discord.Member,time:int,reason):
    role = discord.utils.get(ctx.guild.roles,id=850033230341341244)
    channel = Bot.get_channel(850100716559400960)
    await member.add_roles(role)
    emb = discord.Embed(title="Мут",color=0x2f3136)
    emb.add_field(name='Модератор',value=ctx.message.author.mention,inline=False)
    emb.add_field(name='Нарушитель',value=member.mention,inline=False)
    emb.add_field(name='Причина',value=reason,inline=False)
    emb.add_field(name="Время",value=time,inline=False)
    await channel.send(embed = emb)
    await asyncio.sleep(time*60 )
    emb = discord.Embed(title="Анмут",color=0x2f3136)
    emb.add_field(name='Модератор',value='<@709725675711496233>',inline=False)
    emb.add_field(name='Нарушитель',value=member.mention,inline=False)
    emb.add_field(name='Причина',value="Время мута вышло",inline=False)
    await channel.send(embed=emb)
    await member.remove_roles(role)

@bot.command()
@commands.has_permissions(view_audit_log=True)
async def unmute(ctx,member:discord.Member):
    channel = Bot.get_channel(850100716559400960)
    muterole = discord.utils.get(ctx.guild.roles,id=850033230341341244)
    emb = discord.Embed(title="Анмут",color=0xff0000)
    emb.add_field(name='Модератор',value=ctx.message.author.mention,inline=False)
    emb.add_field(name='Нарушитель',value=member.mention,inline=False)
    await channel.send(embed = emb)
    await member.remove_roles(muterole)

@bot.command()
@commands.has_permissions(view_audit_log=True)
async def kick(ctx,member:discord.Member,reason):
    channel = Bot.get_channel(850100716559400960)
    emb = discord.Embed(title="Кик",color=0xff0000)
    emb.add_field(name='Модератор',value=ctx.message.author.mention,inline=False)
    emb.add_field(name='Нарушитель',value=member.mention,inline=False)
    emb.add_field(name='Причина',value=reason,inline=False)
    await member.kick()
    await channel.send(embed = emb)

@bot.command()
@commands.has_permissions(view_audit_log=True)
async def ban(ctx,member:discord.Member,reason):
    channel = Bot.get_channel(850100716559400960)
    emb = discord.Embed(title="Кик",color=0xff0000)
    emb.add_field(name='Модератор',value=ctx.message.author.mention,inline=False)
    emb.add_field(name='Нарушитель',value=member.mention,inline=False)
    emb.add_field(name='Причина',value=reason,inline=False)
    await member.ban()
    await channel.send(embed = emb)

@bot.command()
async def info(ctx,member:discord.Member):
    emb = discord.Embed(title='Информация о пользователе',color=0xff0000)
    emb.add_field(name="Когда присоединился:",value=member.joined_at,inline=False)
    emb.add_field(name='Имя:',value=member.display_name,inline=False)
    emb.add_field(name='Айди:',value=member.id,inline=False)
    emb.add_field(name="Аккаунт был создан:",value=member.created_at.strftime("%a,%#d %B %Y, %I:%M %p UTC"),inline=False)
    emb.set_thumbnail(url=member.avatar_url)
    emb.set_footer(text=f"Вызвано:{ctx.message.author}",icon_url=ctx.message.author.avatar_url)
    emb.set_author(name=ctx.message.author,icon_url=ctx.message.author.avatar_url)
    await ctx.send(embed = emb)

Arguments = ['feet', 'yuri', 'trap', 'futanari', 'hololewd', 'lewdkemo', 'solog', 'feetg', 'cum', 'erokemo', 'les', 'wallpaper', 'lewdk', 'ngif', 'tickle', 'lewd', 'feed', 'gecg', 'eroyuri', 'eron', 'cum_jpg', 'bj', 'nsfw_neko_gif', 'solo', 'kemonomimi', 'nsfw_avatar', 'gasm', 'poke', 'anal', 'slap', 'hentai', 'avatar', 'erofeet', 'holo', 'keta', 'blowjob', 'pussy', 'tits', 'holoero', 'lizard', 'pussy_jpg', 'pwankg', 'classic', 'kuni', 'waifu', 'pat', '8ball', 'kiss', 'femdom', 'neko', 'spank', 'cuddle', 'erok', 'fox_girl', 'boobs', 'random_hentai_gif', 'smallboobs', 'hug', 'ero', 'smug', 'goose', 'baka', 'woof']
def is_nsfw():
    async def predicate(ctx):
        return ctx.channel.is_nsfw()
    return commands.check(predicate)

@bot.command()
@is_nsfw()
async def cum(ctx):
    emb = discord.Embed(color=0xebebeb)
    emb.set_image(url=nekos.img('cum'))
    await ctx.send(embed=emb)

@bot.event
async def on_ready():
    print('Logged in as:\n{0.user.name}\n{0.user.id}'.format(bot))

    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="в твоё сердце"))

bot.run('ODUwNzIwOTI1NTYwODY0Nzg4.YLt1mg.3h8PsbSz6bOSZa-wad4e2LbrAGg')

