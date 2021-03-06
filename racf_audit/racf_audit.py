# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2017 SML

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import argparse
import asyncio
import json
import os
import re
from collections import defaultdict

import aiohttp
import discord
import unidecode
import yaml
from cogs.utils import checks
from cogs.utils.chat_formatting import pagify, box, inline, underline
from cogs.utils.dataIO import dataIO
from discord.ext import commands
from tabulate import tabulate

PATH = os.path.join("data", "racf_audit")
JSON = os.path.join(PATH, "settings.json")
PLAYERS = os.path.join("data", "racf_audit", "player_db.json")


def nested_dict():
    """Recursively nested defaultdict."""
    return defaultdict(nested_dict)


def server_role(server, role_name):
    """Return discord role object by name."""
    return discord.utils.get(server.roles, name=role_name)


def member_has_role(server, member, role_name):
    """Return True if member has specific role."""
    role = discord.utils.get(server.roles, name=role_name)
    return role in member.roles


class RACFAuditException(Exception):
    pass


class CachedClanModels(RACFAuditException):
    pass


class RACFClan:
    """RACF Clan."""

    def __init__(self, name=None, tag=None, role=None, membership_type=None, model=None):
        """Init."""
        self.name = name
        self.tag = tag
        self.role = role
        self.membership_type = membership_type
        self.model = model

    @property
    def repr(self):
        """Representation of the clan. Used for debugging."""
        o = []
        o.append('RACFClan object')
        o.append(
            "{0.name} #{0.tag} | {0.role.name}".format(self)
        )
        members = sorted(self.model.members, key=lambda m: m.name.lower())
        member_names = [m.name for m in members]
        print(member_names)
        o.append(', '.join(member_names))
        return '\n'.join(o)


class DiscordUser:
    """Discord user = player tag association."""

    def __init__(self, user=None, tag=None):
        """Init."""
        self.user = user
        self.tag = tag


class DiscordUsers:
    """List of Discord users."""

    def __init__(self, crclan_cog, server):
        """Init."""
        self.crclan_cog = crclan_cog
        self.server = server
        self._user_list = None

    @property
    def user_list(self):
        """Create multiple DiscordUser from a list of tags.

        players format:
        '99688854348369920': '22Q0VGUP'
        discord_member_id: CR player tag
        """
        if self._user_list is None:
            players = self.crclan_cog.manager.get_players(self.server)
            out = []
            for member_id, player_tag in players.items():
                user = self.server.get_member(member_id)
                if user is not None:
                    out.append(DiscordUser(user=user, tag=player_tag))
            self._user_list = out
        return self._user_list

    def tag_to_member(self, tag):
        """Return Discord member from tag."""
        for u in self.user_list:
            if u.tag == tag:
                return u.user
        return None

    def tag_to_member_id(self, tag):
        """Return Discord member from tag."""
        for u in self.user_list:
            if u.tag == tag:
                return u.user
        return None


def clean_tag(tag):
    """clean up tag."""
    if tag is None:
        return None
    t = tag
    if t.startswith('#'):
        t = t[1:]
    t = t.strip()
    t = t.upper()
    return t


def get_role_name(role):
    if role is None:
        return ''

    role = role.lower()

    roles_dict = {
        'leader': 'Leader',
        'coleader': 'Co-Leader',
        'elder': 'Elder',
        'member': 'Member'
    }

    if role in roles_dict.keys():
        return roles_dict.get(role)

    return ''


class ClashRoyaleAPIError(Exception):
    def __init__(self, status=None, message=None):
        super().__init__()
        self._status = status
        self._message = message

    @property
    def status(self):
        return self._status

    @property
    def message(self):
        return self._message

    @property
    def status_message(self):
        out = []
        if self._status is not None:
            out.append(str(self._status))
        if self._message is not None:
            out.append(self._message)
        return '. '.join(out)


class ClashRoyaleAPI:
    def __init__(self, token):
        self.token = token

    async def fetch_with_session(self, session, url, timeout=30.0):
        """Perform the actual fetch with the session object."""
        headers = {
            'Authorization': 'Bearer {}'.format(self.token)
        }
        async with session.get(url, headers=headers) as resp:
            async with aiohttp.Timeout(timeout):
                body = await resp.json()
                if resp.status != 200:
                    raise ClashRoyaleAPIError(status=resp.status, message=resp.reason)
        return body

    async def fetch(self, url):
        """Fetch request."""
        error_msg = None
        try:
            async with aiohttp.ClientSession() as session:
                body = await self.fetch_with_session(session, url)
        except asyncio.TimeoutError:
            error_msg = 'Request timed out'
            raise ClashRoyaleAPIError(message=error_msg)
        except aiohttp.ServerDisconnectedError as err:
            error_msg = 'Server disconnected error: {}'.format(err)
            raise ClashRoyaleAPIError(message=error_msg)
        except (aiohttp.ClientError, ValueError) as err:
            error_msg = 'Request connection error: {}'.format(err)
            raise ClashRoyaleAPIError(message=error_msg)
        except json.JSONDecodeError:
            error_msg = "Non JSON returned"
            raise ClashRoyaleAPIError(message=error_msg)
        else:
            return body
        finally:
            if error_msg is not None:
                raise ClashRoyaleAPIError(message=error_msg)

    async def fetch_multi(self, urls):
        """Perform parallel fetch"""
        results = []
        error_msg = None
        try:
            async with aiohttp.ClientSession() as session:
                for url in urls:
                    await asyncio.sleep(0)
                    body = await self.fetch_with_session(session, url)
                    results.append(body)
        except asyncio.TimeoutError:
            error_msg = 'Request timed out'
            raise ClashRoyaleAPIError(message=error_msg)
        except aiohttp.ServerDisconnectedError as err:
            error_msg = 'Server disconnected error: {}'.format(err)
            raise ClashRoyaleAPIError(message=error_msg)
        except (aiohttp.ClientError, ValueError) as err:
            error_msg = 'Request connection error: {}'.format(err)
            raise ClashRoyaleAPIError(message=error_msg)
        except json.JSONDecodeError:
            error_msg = "Non JSON returned"
            raise ClashRoyaleAPIError(message=error_msg)
        else:
            return results
        finally:
            if error_msg is not None:
                raise ClashRoyaleAPIError(message=error_msg)

    async def fetch_clan(self, tag):
        """Get a clan."""
        tag = clean_tag(tag)
        url = 'https://api.clashroyale.com/v1/clans/%23{}'.format(tag)
        body = await self.fetch(url)
        return body

    async def fetch_clan_list(self, tags):
        """Get multiple clans."""
        tags = [clean_tag(tag) for tag in tags]
        urls = ['https://api.clashroyale.com/v1/clans/%23{}'.format(tag) for tag in tags]
        results = await self.fetch_multi(urls)
        return results


class RACFAudit:
    """RACF Audit.
    
    Requires use of additional cogs for functionality:
    SML-Cogs: crclan : CRClan
    SML-Cogs: mm : MemberManagement
    """
    required_cogs = ['crclan', 'mm']

    def __init__(self, bot):
        """Init."""
        self.bot = bot
        self.settings = dataIO.load_json(JSON)
        self._clan_roles = None

        players_path = os.path.join(PATH, PLAYERS)
        if not os.path.exists(players_path):
            players_path = os.path.join(PATH, "player_db_bak.json")
        players = dataIO.load_json(players_path)
        dataIO.save_json(PLAYERS, players)

        with open('data/racf_audit/family_config.yaml') as f:
            self.config = yaml.load(f)

    @property
    def players(self):
        """Player dictionary, userid -> tag"""
        players = dataIO.load_json(PLAYERS)
        return players

    @commands.group(aliases=["racfas"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def racfauditset(self, ctx):
        """RACF Audit Settings."""
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    async def update_server_settings(self, ctx, key, value):
        """Set server settings."""
        server = ctx.message.server
        self.settings[server.id][key] = value
        dataIO.save_json(JSON, self.settings)
        await self.bot.say("Updated settings.")

    async def set_player_tag(self, tag, member: discord.Member, force=False):
        """Allow external programs to set player tags. (RACF)"""
        await asyncio.sleep(0)
        players = self.players
        if tag in players.keys():
            if not force:
                return False
        players[tag] = {
            "tag": clean_tag(tag),
            "user_id": member.id,
            "user_name": member.display_name
        }
        dataIO.save_json(PLAYERS, players)
        return True

    async def get_player_tag(self, tag):
        await asyncio.sleep(0)
        return self.players.get(tag)

    @racfauditset.command(name="auth", pass_context=True)
    @checks.is_owner()
    async def racfauditset_auth(self, ctx, token):
        """Set API Authentication token."""
        self.settings["auth"] = token
        dataIO.save_json(JSON, self.settings)
        await self.bot.say("Updated settings.")
        await self.bot.delete_message(ctx.message)

    @racfauditset.command(name="settings", pass_context=True)
    @checks.is_owner()
    async def racfauditset_settings(self, ctx):
        """Set API Authentication token."""
        await self.bot.say(box(self.settings))

    @property
    def auth(self):
        """API authentication token."""
        return self.settings.get("auth")

    @commands.group(aliases=["racfa"], pass_context=True, no_pm=True)
    async def racfaudit(self, ctx):
        """RACF Audit."""
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @racfaudit.command(name="config", pass_context=True, no_pm=True)
    @checks.mod_or_permissions()
    async def racfaudit_config(self, ctx):
        """Show config."""
        for page in pagify(box(tabulate(self.config['clans'], headers="keys"))):
            await self.bot.say(page)

    def clan_tags(self):
        tags = []
        for clan in self.config.get('clans'):
            # only clans with member roles
            if clan.get('type') == 'Member':
                tags.append(clan.get('tag'))
        return tags

    @property
    def clan_roles(self):
        """Dictionary mapping clan name to clan role names"""
        if self._clan_roles is None:
            self._clan_roles = {}
            for clan in self.config.get('clans'):
                if clan['type'] == 'Member':
                    self._clan_roles[clan['name']] = clan['role_name']
        return self._clan_roles

    def search_args_parser(self):
        """Search arguments parser."""
        # Process arguments
        parser = argparse.ArgumentParser(prog='[p]racfaudit search')

        parser.add_argument(
            'name',
            nargs='?',
            default='_',
            help='IGN')
        parser.add_argument(
            '-c', '--clan',
            nargs='?',
            help='Clan')
        parser.add_argument(
            '-n', '--min',
            nargs='?',
            type=int,
            default=0,
            help='Min Trophies')
        parser.add_argument(
            '-m', '--max',
            nargs='?',
            type=int,
            default=10000,
            help='Max Trophies')
        parser.add_argument(
            '-l', '--link',
            action='store_true',
            default=False
        )

        return parser

    async def family_member_models(self):
        """All family member models."""
        api = ClashRoyaleAPI(self.auth)
        tags = self.clan_tags()
        clan_models = await api.fetch_clan_list(tags)
        members = []
        for clan_model in clan_models:
            for member_model in clan_model.get('memberList'):
                member_model['tag'] = clean_tag(member_model.get('tag'))
                member_model['clan'] = clan_model
                members.append(member_model)
        return members

    @racfaudit.command(name="tag", pass_context=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def racfaudit_tag(self, ctx, member: discord.Member):
        """Find member tag in DB."""
        found = False
        for tag, m in self.players.items():
            if m["user_id"] == member.id:
                await self.bot.say("RACF Audit database: `{}` is associated to `#{}`".format(member, tag))
                found = True

        if not found:
            await self.bot.say("RACF Audit database: Member is not associated with any tags.")

    @racfaudit.command(name="search", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def racfaudit_search(self, ctx, *args):
        """Search for member.

        usage: [p]racfaudit search [-h] [-t TAG] name

        positional arguments:
          name                  IGN

        optional arguments:
          -h, --help            show this help message and exit
          -c CLAN, --clan CLAN  Clan name
          -n MIN --min MIN      Min Trophies
          -m MAX --max MAX      Max Trophies
          -l --link             Display link to cr-api.com
        """
        parser = self.search_args_parser()
        try:
            pargs = parser.parse_args(args)
        except SystemExit:
            await self.bot.send_cmd_help(ctx)
            return

        results = []
        await self.bot.type()

        try:
            member_models = await self.family_member_models()
        except ClashRoyaleAPIError as e:
            await self.bot.say(e.status_message)
            return

        if pargs.name != '_':
            for member_model in member_models:
                # simple search
                if pargs.name.lower() in member_model.get('name').lower():
                    results.append(member_model)
                else:
                    # unidecode search
                    s = unidecode.unidecode(member_model.get('name'))
                    s = ''.join(re.findall(r'\w', s))
                    if pargs.name.lower() in s.lower():
                        results.append(member_model)
        else:
            results = member_models

        # filter by clan name
        if pargs.clan:
            results = [m for m in results if pargs.clan.lower() in m.get('clan_name').lower()]

        # filter by trophies
        results = [m for m in results if pargs.min <= m.get('trophies') <= pargs.max]

        limit = 10
        if len(results) > limit:
            await self.bot.say(
                "Found more than {0} results. Returning top {0} only.".format(limit)
            )
            results = results[:limit]

        if len(results):
            out = []
            for member_model in results:
                clan = member_model.get('clan')
                clan_name = None
                if clan is not None:
                    clan_name = clan.get('name')

                out.append("**{name}** #{tag}, {clan_name}, {role}, {trophies}".format(
                    name=member_model.get('name'),
                    tag=member_model.get('tag'),
                    clan_name=clan_name,
                    role=get_role_name(member_model.get('role')),
                    trophies=member_model.get('trophies')
                ))
                if pargs.link:
                    out.append('<http://cr-api.com/player/{}>'.format(member_model.get('tag')))
            for page in pagify('\n'.join(out)):
                await self.bot.say(page)
        else:
            await self.bot.say("No results found.")

    def run_args_parser(self):
        """Search arguments parser."""
        # Process arguments
        parser = argparse.ArgumentParser(prog='[p]racfaudit run')

        parser.add_argument(
            '-x', '--exec',
            action='store_true',
            default=False,
            help='Execute add/remove roles')
        parser.add_argument(
            '-d', '--debug',
            action='store_true',
            default=False,
            help='Debug')
        parser.add_argument(
            '-c', '--clan',
            nargs='+',
            help='Clan(s) to show')
        parser.add_argument(
            '-s', '--settings',
            action='store_true',
            default=False,
            help='Settings')

        return parser

    @racfaudit.command(name="run", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def racfaudit_run(self, ctx, *args):
        """Audit the entire RACF family.

        [p]racfaudit run [-h] [-x] [-d] [-c CLAN [CLAN ...]]

        optional arguments:
          -h, --help            show this help message and exit
          -x, --exec            Execute add/remove roles
          -d, --debug           Debug
          -c CLAN [CLAN ...], --clan CLAN [CLAN ...]
                                Clan(s) to show
        """
        parser = self.run_args_parser()
        try:
            pargs = parser.parse_args(args)
        except SystemExit:
            await self.bot.send_cmd_help(ctx)
            return

        option_debug = pargs.debug
        option_exec = pargs.exec

        await self.bot.type()

        try:
            member_models = await self.family_member_models()
        except ClashRoyaleAPIError as e:
            await self.bot.say(e.status_message)
            return
        else:
            await self.bot.say("**RACF Family Audit**")
            # Show settings
            if pargs.settings:
                await ctx.invoke(self.racfaudit_config)

            server = ctx.message.server

            # associate Discord user to member
            for member_model in member_models:
                tag = clean_tag(member_model.get('tag'))
                try:
                    discord_id = self.players[tag]["user_id"]
                except KeyError:
                    pass
                else:
                    member_model['discord_member'] = server.get_member(discord_id)

            if option_debug:
                for member_model in member_models:
                    print(member_model.get('tag'), member_model.get('discord_member'))

            """
            Member processing.
    
            """
            audit_results = {
                "elder_promotion_req": [],
                "coleader_promotion_req": [],
                "leader_promotion_req": [],
                "no_discord": [],
                "no_clan_role": [],
                "no_member_role": [],
                "not_in_our_clans": [],
            }

            # find member_models mismatch
            discord_members = []
            for member_model in member_models:
                has_discord = member_model.get('discord_member')
                if has_discord is None:
                    audit_results["no_discord"].append(member_model)

                if has_discord:
                    discord_member = member_model.get('discord_member')
                    discord_members.append(discord_member)
                    # promotions
                    is_elder = False
                    is_coleader = False
                    is_leader = False
                    for r in discord_member.roles:
                        if r.name.lower() == 'elder':
                            is_elder = True
                        if r.name.lower() == 'coleader':
                            is_coleader = True
                        if r.name.lower() == 'leader':
                            is_leader = True
                    if is_elder:
                        if member_model.get('role').lower() != 'elder':
                            audit_results["elder_promotion_req"].append(member_model)
                    if is_coleader:
                        if member_model.get('role').lower() != 'coleader':
                            audit_results["coleader_promotion_req"].append(member_model)
                    if is_leader:
                        if member_model.get('role').lower() != 'leader':
                            audit_results["leader_promotion_req"].append(member_model)

                    # no clan role
                    clan_name = member_model['clan']['name']
                    clan_role_name = self.clan_roles[clan_name]
                    if clan_role_name not in [r.name for r in discord_member.roles]:
                        audit_results["no_clan_role"].append({
                            "discord_member": discord_member,
                            "member_model": member_model
                        })

                    # no member role
                    discord_role_names = [r.name for r in discord_member.roles]
                    if 'Member' not in discord_role_names:
                        audit_results["no_member_role"].append(discord_member)

            # find discord member with roles
            for user in server.members:
                user_roles = [r.name for r in user.roles]
                if 'Member' in user_roles:
                    if user not in discord_members:
                        audit_results['not_in_our_clans'].append(user)

            # show results
            def list_member(member_model):
                """member row"""
                clan = member_model.get('clan')
                clan_name = None
                if clan is not None:
                    clan_name = clan.get('name')

                row = "**{name}** #{tag}, {clan_name}, {role}, {trophies}".format(
                    name=member_model.get('name'),
                    tag=member_model.get('tag'),
                    clan_name=clan_name,
                    role=get_role_name(member_model.get('role')),
                    trophies=member_model.get('trophies')
                )
                return row

            out = []
            for clan in self.config['clans']:
                await self.bot.type()
                await asyncio.sleep(0)

                display_output = False

                if pargs.clan:
                    for c in pargs.clan:
                        if c.lower() in clan['name'].lower():
                            display_output = True
                else:
                    display_output = True

                if not display_output:
                    continue

                if clan['type'] == 'Member':
                    out.append("-" * 40)
                    out.append(inline(clan.get('name')))
                    # no discord
                    out.append(underline("Members without discord"))
                    for member_model in audit_results["no_discord"]:
                        try:
                            if member_model['clan']['name'] == clan.get('name'):
                                out.append(list_member(member_model))
                        except KeyError:
                            pass
                    # elders
                    out.append(underline("Elders need promotion"))
                    for member_model in audit_results["elder_promotion_req"]:
                        try:
                            if member_model['clan']['name'] == clan.get('name'):
                                out.append(list_member(member_model))
                        except KeyError:
                            pass
                    # coleaders
                    out.append(underline("Co-Leaders need promotion"))
                    for member_model in audit_results["coleader_promotion_req"]:
                        try:
                            if member_model['clan']['name'] == clan.get('name'):
                                out.append(list_member(member_model))
                        except KeyError:
                            pass
                    # clan role
                    out.append(underline("No clan role"))
                    for result in audit_results["no_clan_role"]:
                        try:
                            if result["member_model"]['clan']['name'] == clan.get('name'):
                                out.append(result['discord_member'].mention)
                        except KeyError:
                            pass

            # not in our clans
            out.append("-" * 40)
            out.append(underline("Discord users not in our clans but with member roles"))
            for result in audit_results['not_in_our_clans']:
                out.append('`{}` {}'.format(result, result.id))

            for page in pagify('\n'.join(out)):
                await self.bot.say(page)

            if option_exec:
                # change clan roles
                for result in audit_results["no_clan_role"]:
                    try:
                        member_model = result['member_model']
                        discord_member = result['discord_member']
                        clan_role_name = self.clan_roles[member_model['clan']['name']]
                        other_clan_role_names = [r for r in self.clan_roles.values() if r != clan_role_name]
                        for rname in other_clan_role_names:
                            role = discord.utils.get(discord_member.roles, name=rname)
                            if role is not None:
                                await asyncio.sleep(0)
                                await self.bot.remove_roles(discord_member, role)
                                await self.bot.say("Remove {} from {}".format(role, discord_member))

                        role = discord.utils.get(server.roles, name=clan_role_name)
                        await asyncio.sleep(0)
                        await self.bot.add_roles(discord_member, role)
                        await self.bot.say("Add {} to {}".format(role.name, discord_member))
                    except KeyError:
                        pass

                member_role = discord.utils.get(server.roles, name='Member')
                visitor_role = discord.utils.get(server.roles, name='Visitor')
                for discord_member in audit_results["no_member_role"]:
                    try:
                        await asyncio.sleep(0)
                        await self.bot.add_roles(discord_member, member_role)
                        await self.bot.say("Add {} to {}".format(member_role, discord_member))
                        await self.bot.remove_roles(discord_member, visitor_role)
                    except KeyError:
                        pass

                # remove member roles from people who are not in our clans
                for result in audit_results['not_in_our_clans']:
                    result_role_names = [r.name for r in result.roles]
                    # ignore people with special
                    if 'Special' in result_role_names:
                        continue
                    if 'Keep-Member' in result_role_names:
                        continue
                    if 'Leader-Emeritus' in result_role_names:
                        continue

                    to_remove_role_names = []
                    for role_name in ['Member', 'Tourney', 'Practice']:
                        if role_name in result_role_names:
                            to_remove_role_names.append(role_name)
                    to_remove_roles = [discord.utils.get(server.roles, name=rname) for rname in to_remove_role_names]
                    await asyncio.sleep(0)
                    await self.bot.remove_roles(result, *to_remove_roles)
                    await self.bot.say("Removed {} from {}".format(
                        ", ".join(to_remove_role_names), result)
                    )
                    await self.bot.add_roles(result, visitor_role)
                    await self.bot.say("Added Visitor to {}".format(result))

            await self.bot.say("Audit finished.")


def check_folder():
    """Check folder."""
    os.makedirs(PATH, exist_ok=True)
    os.makedirs(os.path.join(PATH, "clans"), exist_ok=True)


def check_file():
    """Check files."""
    if not dataIO.is_valid_json(JSON):
        dataIO.save_json(JSON, {})


def setup(bot):
    """Setup."""
    check_folder()
    check_file()
    n = RACFAudit(bot)
    bot.add_cog(n)
