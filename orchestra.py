import os

import discord
from discord.ext import commands

from discord_slash import SlashCommand

bot = commands.Bot(
  command_prefix="$", #make prefix as its required; wont be used.
  self_bot=True, #silence attempts at message commands
  description='A music bot.',
  help_command=None #remove default help command (we wont need it),
  owner_ids = [
    #   Akins#1692   |   Minister of Commerce
    707643377621008447,
    #   JadenStar10#3620   |   King of Daeltheria
    180127698159534081,
    #   Dagda#5796   |   Member of the Commission on Daeltherian Infrastructure
    634850089214672972
  ]
)

slash = SlashCommand(bot, sync_commands=True)

def main():
  cogs = [
    'plugins.core.errors.error_handling',
    'plugins.music.music',
    'plugins.core.startup.login'
  ]

  for cog in cogs:
    bot.load_extension(cog)
    
  bot.run(os.getenv("TOKEN"))
