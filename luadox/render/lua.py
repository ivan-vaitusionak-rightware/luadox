# Copyright 2023 Jason Tackaberry
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__all__ = ['LuaLSRenderer']

import os
import re
from typing import Callable, List, Optional

from ..log import log
from ..parse import *
from ..reference import *
from ..utils import *
from ..version import __version__
from .base import Renderer

# Maps LuaDox/LDoc-style type names to the types understood by the Lua language
# server (LuaLS / EmmyLua annotations).  Anything not listed here -- most notably
# class names -- is passed through unchanged so cross references keep working.
TYPE_MAP = {
    'bool': 'boolean',
    'int': 'integer',
    'float': 'number',
    'double': 'number',
    'void': 'nil',
}

# Default type assigned to a documented field that lacks an explicit @type.
DEFAULT_FIELD_TYPE = 'any'
# Default type assigned to members of a @table.  Tables in LuaDox are predominantly
# used to declare enumerations whose members are integers.
DEFAULT_TABLE_FIELD_TYPE = 'integer'

# Matches the markdown links the prerender stage generates for cross references,
# whose target is an opaque 'luadox:<id>'.  The language server can't resolve these,
# so they are reduced to their visible link text.
RE_LUADOX_LINK = re.compile(r'\[([^\]]*)\]\(luadox:[^)]*\)')


class LuaLSRenderer(Renderer):
    """
    Renders parsed content as a Lua definition file annotated with LuaLS / EmmyLua
    (---@) annotations.

    The output is a single self-contained ``---@meta`` file that the Lua language
    server can consume to provide editor features -- completion, hover docs, and
    signature help -- for code that uses the documented (typically native) API,
    without that API having any runtime Lua implementation of its own.
    """

    def _strip_links(self, md: str) -> str:
        """
        Reduces 'luadox:' cross-reference links to their visible text, leaving any
        other markdown (including regular links) intact.
        """
        return RE_LUADOX_LINK.sub(r'\1', md)

    def _map_type(self, types: List[str]) -> str:
        """
        Converts a list of LuaDox type names into a single LuaLS type expression,
        translating known primitives and joining alternatives with '|'.
        """
        mapped: List[str] = []
        for entry in types:
            for part in entry.split('|'):
                part = part.strip()
                if part:
                    mapped.append(TYPE_MAP.get(part, part))
        return '|'.join(mapped) if mapped else 'any'

    def _content_to_lines(self, content: Content) -> List[str]:
        """
        Flattens Content into a list of markdown lines suitable for a doc comment,
        with cross-reference links reduced to plain text.
        """
        lines: List[str] = []
        for elem in content:
            if isinstance(elem, Markdown):
                lines.extend(self._strip_links(elem.get()).split('\n'))
            elif isinstance(elem, Admonition):
                title = self._strip_links(elem.title) if elem.title else elem.type.title()
                lines.append('**{}**'.format(title))
                lines.extend(self._content_to_lines(elem.content))
            elif isinstance(elem, SeeAlso):
                names = []
                for refid in elem.refs:
                    ref = self.parser.refs_by_id.get(refid)
                    names.append(ref.name if ref else refid)
                if names:
                    lines.append('See also: ' + ', '.join(names))
        return lines

    def _inline(self, content: Content) -> str:
        """
        Collapses Content into a single line, used for @param/@return descriptions
        which the language server expects on one line.
        """
        text = ' '.join(self._content_to_lines(content))
        return re.sub(r'\s+', ' ', text).strip()

    def _emit_doc(self, out: Callable[[str], None], lines: List[str]) -> None:
        """
        Emits the given markdown lines as a '---' prefixed doc comment, trimming
        surrounding blank lines.
        """
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        for line in lines:
            out('---' + line if line.strip() else '---')

    def _field_lhs(self, ref: FieldRef) -> str:
        """
        Returns the Lua assignment target for a field.  Fields scoped directly to an
        implicit module are globals (use the bare symbol), otherwise the fully
        qualified name gives the real access path (e.g. ActivityStatus.Active).
        """
        scope = ref.scope
        if isinstance(scope, ModuleRef) and scope.implicit:
            return ref.symbol
        return ref.name

    def _emit_field(self, out: Callable[[str], None], ref: FieldRef, default_type: str) -> None:
        self.ctx.update(ref=ref)
        lines = self._content_to_lines(ref.content)
        if ref.meta:
            lines.append('*{}*'.format(self._strip_links(ref.meta)))
        self._emit_doc(out, lines)
        typ = self._map_type(ref.types) if ref.types else default_type
        out('---@type {}'.format(typ))
        out('{} = nil'.format(self._field_lhs(ref)))
        out('')

    def _emit_function(self, out: Callable[[str], None], ref: FunctionRef) -> None:
        self.ctx.update(ref=ref)
        self._emit_doc(out, self._content_to_lines(ref.content))
        for name, types, doc in ref.params:
            line = '---@param {} {}'.format(name, self._map_type(types))
            desc = self._inline(doc)
            if desc:
                line += ' ' + desc
            out(line)
        for types, doc in ref.returns:
            line = '---@return {}'.format(self._map_type(types))
            desc = self._inline(doc)
            if desc:
                line += ' # ' + desc
            out(line)
        params = ', '.join(name for name, _, _ in ref.params)
        # ref.symbol is the real source-level callable (Class:method, Class.func, or a
        # bare global), so it produces the correct definition in all cases.
        out('function {}({}) end'.format(ref.symbol, params))
        out('')

    def _emit_members(self, out: Callable[[str], None], col: CollectionRef, default_type: str) -> None:
        for ref in col.fields:
            self._emit_field(out, ref, default_type)
        for ref in col.functions:
            self._emit_function(out, ref)

    def _emit_table(self, out: Callable[[str], None], col: CollectionRef) -> None:
        self.ctx.update(ref=col)
        # For tables the leading sentence was popped into the heading during prerender,
        # so recombine them for the table's doc comment.
        lines = [self._strip_links(col.heading)] if col.heading else []
        lines.extend(self._content_to_lines(col.content))
        self._emit_doc(out, lines)
        out('{} = {{}}'.format(col.name))
        out('')
        self._emit_members(out, col, DEFAULT_TABLE_FIELD_TYPE)

    def _emit_class(self, out: Callable[[str], None], topref: ClassRef) -> None:
        self.ctx.update(ref=topref)
        self._emit_doc(out, self._content_to_lines(topref.content))
        decl = '---@class {}'.format(topref.name)
        parent = topref.flags.get('inherits')
        if parent:
            decl += ' : {}'.format(parent)
        out(decl)
        out('{} = {{}}'.format(topref.name))
        out('')
        for col in topref.collections:
            self.ctx.update(ref=col)
            if isinstance(col, TableRef):
                self._emit_table(out, col)
            else:
                self._emit_members(out, col, DEFAULT_FIELD_TYPE)

    def _emit_module(self, out: Callable[[str], None], topref: ModuleRef) -> None:
        self.ctx.update(ref=topref)
        # Implicit modules aren't real Lua tables (they stand in for a source file), so
        # their members are emitted as globals.  Explicit modules get a backing table so
        # qualified members (module.foo) resolve.
        if not topref.implicit:
            self._emit_doc(out, self._content_to_lines(topref.content))
            out('{} = {{}}'.format(topref.name))
            out('')
        for col in topref.collections:
            self.ctx.update(ref=col)
            if isinstance(col, TableRef):
                self._emit_table(out, col)
            else:
                self._emit_members(out, col, DEFAULT_FIELD_TYPE)

    def _get_outfile(self, dst: Optional[str], ext: str = '.lua') -> str:
        if not dst:
            dst = './luadox' + ext
            log.warn('"out" is not defined in config file, assuming %s', dst)
        if not os.path.isfile(dst) and not dst.endswith(ext):
            dst = os.path.join(dst, 'luadox' + ext)
        dirname = os.path.dirname(dst)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)
        log.info('rendering to %s', dst)
        return dst

    def render(self, toprefs: List[TopRef], dst: Optional[str]) -> None:
        """
        Renders toprefs as a single LuaLS definition file at the given output path
        (or directory, in which case luadox.lua is written into it).
        """
        lines: List[str] = []
        out = lines.append
        out('---@meta')
        out('-- Lua API definitions generated by LuaDox {}.'.format(__version__))
        title = (self.config.get('project', 'title', fallback=None)
                 or self.config.get('project', 'name', fallback=None))
        if title:
            out('-- {}'.format(title))
        out('-- This file is generated. Do not edit.')
        out('')

        for topref in toprefs:
            if topref.userdata.get('empty') and topref.implicit:
                # No documented content and implicitly generated, so nothing to emit.
                continue
            if isinstance(topref, ClassRef):
                self._emit_class(out, topref)
            elif isinstance(topref, ModuleRef):
                self._emit_module(out, topref)
            # ManualRefs are prose pages with no API surface, so they're skipped.

        outfile = self._get_outfile(dst)
        with open(outfile, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines).rstrip() + '\n')
