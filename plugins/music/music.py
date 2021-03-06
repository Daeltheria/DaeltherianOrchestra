#define guild ids. The ID for Daeltheria is in here, however this can be changed by adding in your own, or replacing Daeltheria's with your own.
guild_ids = [419303709584130048]

import asyncio
import functools
import itertools
import math
import random

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands

from discord_slash import cog_ext, SlashContext
from discord_slash.utils.manage_commands import create_option

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

    def __init__(self, ctx: SlashContext, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
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
    async def create_source(cls, ctx: SlashContext, search: str, *, loop: asyncio.BaseEventLoop = None):
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
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='{0.source.title}'.format(self),
                               description='```\nNow playing...\n```',
                               color=discord.Color.dark_purple(),
                               url="{0.source.url}".format(self)
                              )
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Requested by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
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
    def __init__(self, bot: commands.Bot, ctx: SlashContext):
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
        self.name="Music"

    def get_voice_state(
      self,
      ctx: SlashContext
    ):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(
      self,
      ctx: SlashContext
    ):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: SlashContext):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: SlashContext, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @cog_ext.cog_slash(
      name="join",
      description="Joins the current voice channel.",
      guild_ids=guild_ids
    )
    async def _join(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if ctx.guild.voice_client and ctx.guild.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Bot is already in a voice channel.')

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @cog_ext.cog_slash(
      name="summon",
      description="Summons the bot to a given channel.",
      options = [
        create_option(
          name="channel",
          description="The channel you want the bot to be summoned to.",
          option_type=7,
          required=True
        )
      ],
      guild_ids=guild_ids
    )
    async def _summon(
      self,
      ctx: SlashContext,
      channel: discord.VoiceChannel
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if not channel:
            raise VoiceError('You are neither connected to a voice channel nor specified a channel to join.')

        destination = channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @cog_ext.cog_slash(
      name="disconnect",
      description="Disconnects the bot from your current channel.",
      guild_ids=guild_ids
    )
    async def _leave(
      self,
      ctx: SlashContext
    ):

        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to any voice channel.')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @cog_ext.cog_slash(
      name="volume",
      description="Allows you to set the volume of a command.",
      options = [
        create_option(
          name="volume",
          description="The value you want to set the volume to.",
          option_type=4,
          required=True
        )
      ],
      guild_ids=guild_ids
    )
    async def _volume(
      self,
      ctx: SlashContext,
      volume: int
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        if 0 > volume > 100:
            return await ctx.send('Volume must be between 0 and 100')

        ctx.voice_state.volume = volume / 100
        await ctx.channel.send('Volume of the player set to {}%'.format(volume))

    @cog_ext.cog_slash(
      name="now-playing",
      description="Displays the song that is currently playing.",
      guild_ids=guild_ids
    )
    async def _now(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        await ctx.channel.send(embed=ctx.voice_state.current.create_embed())

    @cog_ext.cog_slash(
      name="pause",
      description="Pauses the current song.",
      guild_ids=guild_ids
    )
    async def _pause(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.channel.send("Song paused.")

    @cog_ext.cog_slash(
      name="resume",
      description="Resumes the current song.",
      guild_ids=guild_ids
    )
    async def _resume(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.channel.send("Song resumed.")

    @cog_ext.cog_slash(
      name="stop",
      description="Stops the currently playing song and clears the queue.",
      guild_ids=guild_ids
    )
    async def _stop(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        ctx.voice_state.songs.clear()

        if not ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.channel.send('???')

    @cog_ext.cog_slash(
      name="skip",
      description="Skips the currently playing song.",
      guild_ids=guild_ids
    )
    async def _skip(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.author
        if voter == ctx.voice_state.current.requester or len(ctx.voice_state.channel.members) <= 3:
            await ctx.channel.send("Song skipped.")
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.send('???')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/3**'.format(total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @cog_ext.cog_slash(
      name="queue",
      description="Displays the bot's queue.",
      options = [
        create_option(
          name="page",
          description="The page of the queue that you want to view.",
          option_type=4,
          required=False
        )
      ],
      guild_ids=guild_ids
    )
    async def _queue(
      self,
      ctx: SlashContext,
      page: int = 1
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

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

    @cog_ext.cog_slash(
      name="shuffle",
      description="Shuffles the queue.",
      guild_ids=guild_ids
    )
    async def _shuffle(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.send('???')

    @cog_ext.cog_slash(
      name="remove",
      description="Removes an item from the queue.",
      options = [
        create_option(
          name="index",
          description="The index of the song you want to remove from the queue.",
          option_type=4,
          required=True
        )
      ],
      guild_ids=guild_ids
    )
    async def _remove(
      self,
      ctx: SlashContext,
      index: int
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.send('???')

    @cog_ext.cog_slash(
      name="loop",
      description="Loops or unloops the queue depending on the state of the loop.",
      guild_ids=guild_ids
    )
    async def _loop(
      self,
      ctx: SlashContext
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.send('???')

    @cog_ext.cog_slash(
      name="play",
      description="Plays a given url or title.",
      options = [
        create_option(
          name="query",
          description="What you want to play or search for",
          option_type=3,
          required=True
        )
      ],
      guild_ids=guild_ids
    )
    async def _play(
      self,
      ctx: SlashContext,
      query: str
    ):
        ctx.voice_state = self.get_voice_state(ctx)
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)
        
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('You are not connected to any voice channel.')

        await ctx.defer()
        try:
          source = await YTDLSource.create_source(ctx, query, loop=self.bot.loop)
        except YTDLError as e:
          await ctx.send('An error occurred while processing this request: {}'.format(str(e)))
        else:
          song = Song(source)

        await ctx.voice_state.songs.put(song)
        await ctx.send('Enqueued {}'.format(str(source)))


def setup(bot: commands.Bot):
  bot.add_cog(Music(bot))
