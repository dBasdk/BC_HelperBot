import re
import io
import json
import asyncio
from urllib import request
from functools import partial
from datetime import datetime
from collections import OrderedDict

import discord
from discord.ext import commands
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter

from .utils import custom_errors, checkers, misc
from .utils.i18n import use_current_gettext as _

RE_EVENT_DATE = re.compile(r'(?<=event-date : )(\d{,2})/(\d{,2})/(\d{4})')
RE_EVENT_STATE = re.compile(r'(?<=event-state : )(\S+)')
RE_EVENT_NAME = re.compile(r'(?<=event-name : )(.+)')
RE_EVENT_AUTOTESTS_GROUP = re.compile(r'event-autotests : \[\[\n(.*)\n]]', re.MULTILINE | re.DOTALL)
RE_EVENT_AUTOTEST = re.compile(r'{(.*?)} : \[\s*(.*?)\s*]', re.MULTILINE | re.DOTALL)

RE_GET_CODE_PARTICIPATION = re.compile(r'(```)?(?:(\S*)\s)(\s*\S[\S\s]*)(?(1)```|)')
RE_ENDLINE_SPACES = re.compile(r' *\n')


CODE_CHANNEL_ID = 810511403202248754

with request.urlopen('https://emkc.org/api/v1/piston/versions') as r:
    AVAILABLE_LANGUAGES: list = json.loads(r.read().decode('utf-8'))

LANGUAGES_EQUIVALENT = {
    ('node', 'typescript', 'deno'): 'javascript',
    ('cpp', 'c'): 'c++',
    ('nasm', 'nasm64'): 'nasm',
    ('python2', 'python3'): 'python'
}


def event_not_closed():
    async def inner(ctx):
        code_channel = ctx.bot.get_channel(CODE_CHANNEL_ID)
        state = RE_EVENT_STATE.search(code_channel.topic).group()

        if state == 'closed':
            await ctx.bot.set_actual_language(ctx.author)
            await ctx.send(_('There is no event right now, sorry !'), delete_after=5)
            return False

        return True

    return commands.check(inner)


def event_not_ended():
    async def inner(ctx):
        code_channel = ctx.bot.get_channel(CODE_CHANNEL_ID)
        state = RE_EVENT_STATE.search(code_channel.topic).group()

        if state == 'ended':
            await ctx.bot.set_actual_language(ctx.author)
            await ctx.send(_('The event is ended, sorry !'), delete_after=5)
            return False

        return True

    return commands.check(inner)


class Event(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.code_channel_id = 810511403202248754

    @commands.group(
        name='event',
        description=_('Participate or get informations about an event.'),
        invoke_without_command=True
    )
    async def event(self, ctx):
        if ctx.guild and ctx.channel.id not in self.bot.test_channels_id:  # Not in dm or in tests channels
            raise custom_errors.NotAuthorizedChannels(self.bot.test_channels_id)

        embed = discord.Embed(
            title=_("Use of /event"),
            color=misc.Color.grey_embed().discord
        )

        for command in ctx.command.commands:
            if command.hidden: continue
            embed.add_field(name=f"** • {command.name} : {_(command.description)}**", value=f"`{command.usage}`", inline=False)

        await ctx.send(embed=embed)

    async def get_participations(self, user=None) -> (dict, list, dict):
        code_channel = self.bot.get_channel(self.code_channel_id)
        event_informations = self.get_informations()

        datas = dict()
        datas_global = []
        user_infos = dict()

        async for message in code_channel.history(limit=None, after=event_informations['date']):
            if message.author.id != self.bot.user.id or not message.embeds: continue

            fields = message.embeds[0].fields

            try: code_author = self.bot.get_user(user_id := int(fields[0].value.split('|')[0])) or await self.bot.fetch_user(user_id)
            except: continue

            language = fields[1].value
            length = int(fields[2].value)
            date = datetime.fromisoformat(fields[3].value)

            infos = (message, code_author, length, date)

            if user and user.id == code_author.id:
                user_infos[language] = infos

            datas.setdefault(language, [])
            datas[language].append(infos)
            datas_global.append(infos)

        sort_key = lambda obj: obj[2:3]  # length and date

        datas = {key: sorted(value, key=sort_key) for key, value in datas.items()}
        datas_global = sorted(datas_global, key=sort_key)

        return datas, datas_global, user_infos

    def get_informations(self):
        channel = self.bot.get_channel(CODE_CHANNEL_ID)

        state = RE_EVENT_STATE.search(channel.topic).group()

        day, month, year = RE_EVENT_DATE.search(channel.topic).groups()
        date = datetime(int(year), int(month), int(day) - 1)  # remove one date to use properly the after param

        name = RE_EVENT_NAME.search(channel.topic).group()

        autotests_group = RE_EVENT_AUTOTESTS_GROUP.search(channel.topic).group()
        autotests = RE_EVENT_AUTOTEST.findall(autotests_group)

        return {'state': state, 'date': date, 'name': name, 'autotests': autotests}

    async def edit_informations(self, state=None, date=None, name=None):
        channel: discord.TextChannel = self.bot.get_channel(CODE_CHANNEL_ID)
        new_topic = channel.topic

        if state:
            new_topic = RE_EVENT_STATE.sub(state, new_topic)
        if date:
            new_topic = RE_EVENT_DATE.sub(date.strftime("%d/%m/%Y"), new_topic)
        if name:
            new_topic = RE_EVENT_NAME.sub(name, new_topic)

        await channel.edit(topic=new_topic)

    @event.command(
        name="participate",
        description=_("Participate to the contest !"),
        usage="/event participate {code}"
    )
    @commands.dm_only()
    @event_not_ended()
    @event_not_closed()
    async def participate(self, ctx, *, code):
        code_channel = self.bot.get_channel(self.code_channel_id)

        re_match = RE_GET_CODE_PARTICIPATION.search(code)
        if not re_match:
            raise commands.CommandError(_('Your message must contains a block of code (with code language) ! *look `/tag discord markdown`*'))
        language, code = re_match.groups()[1:]
        code = code.strip()
        if len(code) > 1000:
            return await ctx.send(_("Looks like your code is too long! Try to remove the useless parts, the goal is to have a short and optimized code!"))

        language = discord.utils.find(lambda i: language.lower() in i['aliases'], AVAILABLE_LANGUAGES)
        if not language:
            return await ctx.send(_('Your language seems not be valid for the event.'))

        __, __, user_infos = await self.get_participations(user=ctx.author)
        old_participation: discord.Message = obj[0] if (obj := user_infos.get(language['name'])) else None

        aliased_language = discord.utils.find(lambda couple: language['name'] in couple[0], LANGUAGES_EQUIVALENT.items())
        if aliased_language:
            language = discord.utils.find(lambda i: aliased_language[1] == i['name'], AVAILABLE_LANGUAGES) or language

        valid_message = await ctx.send(_('**This is your participation :**\n\n') +
                                       _('`Language` -> `{0}`\n').format(language['name']) +
                                       _('`Length` -> `{0}`\n').format(len(code)) +
                                       f'```{language["name"]}\n{code}```\n' +
                                       _('Do you want ot post it ? ✅ ❌'))

        self.bot.loop.create_task(misc.add_reactions(valid_message, ['✅', '❌']))

        try: reaction, user = await self.bot.wait_for('reaction_add', check=lambda react, usr: not usr.bot and react.message.id == valid_message.id and str(react.emoji) in ['✅', '❌'], timeout=120)
        except asyncio.TimeoutError: return

        if str(reaction.emoji) == '✅':
            event_informations = self.get_informations()

            if autotests := event_informations['autotests']:
                embed = discord.Embed(title=_('<a:typing:832608019920977921> Your code is passing some tests...'),
                                      description='\n'.join(f'➖ Test {i+1}/{len(autotests)}' for i in range(len(autotests))),
                                      color=misc.Color.grey_embed().discord)

                testing_message: discord.Message = await ctx.send(embed=embed)

                for i, (args, result) in enumerate(autotests):
                    try: execution_result = await misc.execute_piston_code(language['name'], code, args=args.split('|'))
                    except Exception: return await testing_message.edit(content=_('An error occurred.'))

                    if error_message := execution_result.get('stderr'):
                        embed.title = _('Your code excited with an error.')
                        embed.description = f'```\n{error_message[:2000]}\n```'
                        embed.colour = misc.Color(255, 100, 100).discord

                        return await testing_message.edit(embed=embed)

                    stdout = execution_result['stdout'].strip()
                    stdout = RE_ENDLINE_SPACES.sub('\n', stdout)
                    if stdout != result:
                        embed.title = _("Your code didn't pass all the tests. If you think it's an error, please contact a staff.")
                        embed.colour = misc.Color(255, 100, 100).discord

                        description_lines = embed.description.split('\n')
                        description_lines[i] = f'❌ Test {i+1}/{len(autotests)}'
                        embed.description = '\n'.join(description_lines)

                        return await testing_message.edit(embed=embed)

                    description_lines = embed.description.split('\n')
                    description_lines[i] = f'✅ Test {i + 1}/{len(autotests)}'
                    embed.description = '\n'.join(description_lines)
                    await testing_message.edit(embed=embed)

                    await asyncio.sleep(1)

                embed.title = _('All tests passed successfully.')
                embed.colour = misc.Color(100, 255, 100).discord

                await testing_message.edit(embed=embed)

            embed = discord.Embed(
                title="Participation :",
                color=misc.Color.grey_embed().discord
            )
            embed.add_field(name='User', value=f'{ctx.author.id}|{ctx.author.mention}', inline=False)
            embed.add_field(name='Language', value=language['name'], inline=True)
            embed.add_field(name='Length', value=str(len(code)), inline=True)
            embed.add_field(name='Date', value=str(datetime.now().isoformat()), inline=False)
            embed.add_field(name='Code', value=f"```{language['name']}\n{code}\n```", inline=False)

            if old_participation:
                await old_participation.edit(embed=embed)
                await old_participation.clear_reactions()
                response = _("Your entry has been successfully modified !")
            else:
                await code_channel.send(embed=embed)
                response = _("Your entry has been successfully sent !")

            try: await ctx.send(response)
            except: pass
        else:
            try: await ctx.send(_('Cancelled'))
            except: pass  # prevent error if the user close his MP

    @event.command(
        name='cancel',
        description=_('Remove your participation from the contest'),
        usage="/event cancel"
    )
    @event_not_ended()
    @event_not_closed()
    async def cancel(self, ctx):
        if ctx.guild and ctx.channel.id not in self.bot.test_channels_id:  # Not in dm or in tests channels
            raise custom_errors.NotAuthorizedChannels(self.bot.test_channels_id)

        __, __, user_infos = await self.get_participations(user=ctx.author)
        if not user_infos:
            return await ctx.send(_("You didn't participate !"))

        if len(user_infos) == 1:
            old_participation: discord.Message = list(user_infos.values())[0][0]
        else:

            reactions = ['0️⃣', '1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣']

            selectable = OrderedDict(user_infos)
            message = await ctx.send(_("__Choose which participation you want to cancel :__\n")+'\n'.join([f"{reactions[i]} - `{language}`" for i, language in enumerate(selectable.keys())]))
            self.bot.loop.create_task(misc.add_reactions(message, reactions[:len(selectable)]))

            try:
                reaction, __ = await self.bot.wait_for('reaction_add', timeout=120,
                                                       check=lambda react, usr: str(react.emoji) in reactions[:len(selectable)] and usr.id == ctx.author.id and react.message.id == message.id)
            except TimeoutError:
                try: await message.delete()
                except: pass
                return

            try: await message.clear_reactions()
            except: pass

            old_participation: discord.Message = list(user_infos.values())[reactions.index(str(reaction.emoji))][0]

        await old_participation.delete()
        await ctx.send(_('Your participation has been successfully deleted'))

    @event.command(
        name="stats",
        description=_("Get some stats about the current contest"),
        usage="/event stats"
    )
    @event_not_closed()
    async def stats(self, ctx):
        if ctx.guild and ctx.channel.id not in self.bot.test_channels_id:  # Not in dm or in tests channels
            raise custom_errors.NotAuthorizedChannels(self.bot.test_channels_id)

        datas, datas_global, user_infos = await self.get_participations(ctx.author)

        if not datas:
            return await ctx.send(_("There is no participation at the moment."))

        embed = discord.Embed(
            title=_('Some informations...'),
            color=misc.Color.grey_embed().discord,
            description=_('**Number of participations :** {}\n\u200b').format(len(datas_global))
        )

        for language, data in user_infos.items():
            global_ranking = datas_global.index(data) + 1
            language_ranking = datas[language].index(data) + 1

            formatted_informations = _("• Global ranking : **{}** *({} > you > {})*\n").format(
                global_ranking,
                datas_global[global_ranking][2] if len(datas_global) > global_ranking else _('nobody'),
                datas_global[global_ranking - 2][2] if global_ranking - 1 else _('nobody')  # if rang index isn't 0
            )

            formatted_informations += _("• By language ranking : **{}** *({} > you > {})*").format(
                language_ranking,
                datas[language][language_ranking][2] if len(datas[language]) > language_ranking else _('nobody'),
                datas[language][language_ranking - 2][2] if language_ranking - 1 else _('nobody')  # if rang index isn't 0
            )

            embed.add_field(name=_('Your participation with {}').format(language),
                            value=formatted_informations,
                            inline=False)

        embed.set_image(url="attachment://graph.png")

        fn = partial(self.create_graph_bars, datas, _("Breakdown by languages used."))
        final_buffer = await self.bot.loop.run_in_executor(None, fn)

        file = discord.File(filename="graph.png", fp=final_buffer)

        await ctx.channel.send(embed=embed, file=file)

    @staticmethod
    def create_graph_bars(datas, title):  # title in arguments because translations doesn't work in a separated thread
        fig, ax = plt.subplots()
        langs = datas.keys()
        values = [len(v) for v in datas.values()]
        ax.bar(langs, values,
               color=misc.Color(10, 100, 255, 0.5).mpl,
               edgecolor=misc.Color(10, 100, 255).mpl,
               linewidth=5)

        ax.yaxis.set_major_formatter(StrMethodFormatter('{x:,.0f}'))  # No decimal places
        ax.set_yticks(range(1, max(values) + 1))
        ax.set_title(title)
        buff = io.BytesIO()
        fig.savefig(buff)
        buff.seek(0)
        del fig

        return buff

    @event.command(
        name='start',
        usage='/event start <event_name>',
        hidden=True
    )
    @checkers.is_high_staff()
    async def start(self, ctx, *, name):
        code_channel: discord.TextChannel = self.bot.get_channel(self.code_channel_id)

        await self.edit_informations(state='open', date=datetime.now(), name=name)

        await ctx.send(f'Event `{name}` started ! Participations are now open !')
        await code_channel.send('```diff\n'
                                f'- {name.upper()}\n'
                                '```')

    @event.command(
        name='stop',
        usage='/event stop',
        hidden=True
    )
    @checkers.is_high_staff()
    async def stop(self, ctx):
        await self.edit_informations(state='ended')

        event_informations = self.get_informations()
        datas, datas_global, *__ = await self.get_participations()

        medals = ['🥇', '🥈', '🥉']
        formatted_text = ("```diff\n"
                          "- GLOBAL RANKING\n"
                          "```\n"
                          "{0}\n\n"
                          "```diff\n"
                          "- RANKING BY LANGUAGE\n"
                          "```\n")

        formatted_text = formatted_text.format(
            '\n'.join(
                " {medal} {obj[1].mention} ({obj[1]}) - {obj[2]} chars".format(medal=medals[i], obj=datas_global[i]) for i in range(min(3, len(datas_global)))
            )
        )

        for language, data in datas.items():
            formatted_text += ("> ```diff\n"
                               f"> + {language.upper()}\n"
                               "> ```\n")
            for i in range(min(len(data), 3)):
                formatted_text += f"> {medals[i]} {data[i][1].mention} ({data[i][1]}) - {data[i][2]} chars\n"

            formatted_text += '\n'

        buffer = io.StringIO(formatted_text)
        buffer.seek(0)

        await ctx.send(f"Event `{event_informations['name']}` is now ended ! Participations are closed !", file=discord.File(buffer, 'ranking.txt'))

    @event.command(
        name='close',
        usage='/event close',
        hidden=True
    )
    @checkers.is_high_staff()
    async def close(self, ctx):
        await self.edit_informations(state='closed')
        event_informations = self.get_informations()

        await ctx.send(f"Event `{event_informations['name']}` is now closed !")


def setup(bot):
    bot.add_cog(Event(bot))
