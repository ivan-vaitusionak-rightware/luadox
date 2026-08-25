# Copyright 2021-2023 Jason Tackaberry
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
from typing import Callable, List, Optional, Tuple

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

# Default type assigned to a documented field or table member that lacks an explicit
# @type.  'any' is permissive: it never produces a false type error on correct code.
DEFAULT_FIELD_TYPE = 'any'

# Matches the markdown links the prerender stage generates for cross references,
# whose target is an opaque 'luadox:<id>'.  The language server can't resolve these,
# so they are reduced to their visible link text.
RE_LUADOX_LINK = re.compile(r'\[([^\]]*)\]\(luadox:[^)]*\)')

# Type names the language server understands without any declaration.
LUALS_BUILTIN_TYPES = {
    'any', 'boolean', 'string', 'number', 'integer', 'function', 'table',
    'thread', 'userdata', 'lightuserdata', 'nil', 'unknown', 'self',
}


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
        translating known primitives, qualifying documented class and table names,
        and joining alternatives with '|'.
        """
        mapped: List[str] = []
        for entry in types:
            for part in entry.split('|'):
                part = part.strip()
                if not part:
                    continue
                # C++-style scoped names sometimes leak into bindings
                # documentation.  LuaLS silently reads everything up to '::' as
                # the type, so the parameter would be typed as the enclosing
                # class.  That is a documentation source bug: report it and emit
                # the name verbatim rather than repairing it silently.
                if '::' in part:
                    self.parser.diagnostics.add(
                        'types',
                        'type name "{}" uses C++ scope syntax; use "."'.format(part),
                        self.ctx.file, self.ctx.line
                    )
                    mapped.append(part)
                    continue
                if part in TYPE_MAP:
                    mapped.append(TYPE_MAP[part])
                    continue
                # Resolve names relative to the current scope so an unqualified
                # name (e.g. FieldOfViewType inside Matrix4x4 docs) maps to the
                # fully qualified class or table declaration emitted elsewhere in
                # the file. Non-type references (fields, functions) that happen to
                # share the name must not hijack a type position, so anything else
                # passes through as written -- reported when a plain name neither
                # resolves nor is a language-server built-in, since the language
                # server cannot diagnose it beyond undefined-doc-name.
                ref = self.parser.resolve_ref(part)
                if isinstance(ref, (ClassRef, TableRef)):
                    mapped.append(ref.name)
                    continue
                if part not in LUALS_BUILTIN_TYPES and re.fullmatch(r'[A-Za-z_][\w.]*', part):
                    self.parser.diagnostics.add(
                        'types',
                        'type name "{}" does not resolve to a documented class '
                        'or table'.format(part),
                        self.ctx.file, self.ctx.line
                    )
                mapped.append(part)
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
            if not types:
                # The parameter exists in the signature but was never given a type,
                # so it can only be rendered as 'any' -- report the documentation gap.
                self.parser.diagnostics.add(
                    'untyped',
                    'parameter "{}" of {} has no documented type'.format(name, ref.symbol),
                    self.ctx.file, self.ctx.line
                )
            line = '---@param {} {}'.format(name, self._map_type(types))
            desc = self._inline(doc)
            if desc:
                line += ' ' + desc
            out(line)
        if ref.returns:
            for types, doc in ref.returns:
                line = '---@return {}'.format(self._map_type(types))
                desc = self._inline(doc)
                if desc:
                    line += ' # ' + desc
                out(line)
        else:
            # With no documented return the empty definition body makes the language
            # server infer a nil return and reject `local x = obj:create()`, so fall back
            # to the same permissive 'any' the fields use rather than claim nil.
            out('---@return {}'.format(DEFAULT_FIELD_TYPE))
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
        # Model the table as a class so its name resolves in a type position (e.g.
        # `@treturn SomeEnum`) and its members can be accessed.  Members default to the
        # same permissive 'any' as class fields: a @table is not necessarily an integer
        # enumeration -- many are records of typed values (e.g. property tables) -- so a
        # blanket integer default produced false type errors on correct code.
        out('---@class {}'.format(col.name))
        out('{} = {{}}'.format(col.name))
        out('')
        self._emit_members(out, col, DEFAULT_FIELD_TYPE)

    def _doc_mixins(self, topref: ClassRef) -> List[str]:
        """
        Extracts additional parent classes named in a configured doc phrase.

        Some APIs document runtime composition in prose, e.g. Kanzi metadata classes
        say "Inherits properties and message types from @{A}, @{B}, @{C}." -- including
        mixin/"concept" classes that don't appear in the single-inheritance chain.
        When 'mixin_doc_phrase' is configured, the cross references on the line carrying
        that phrase are resolved to class names and treated as parents, so members
        provided by those classes resolve transitively.
        """
        if not self._mixin_phrase:
            return []
        names: List[str] = []
        for elem in topref.content:
            if not isinstance(elem, Markdown):
                continue
            for line in elem.get().split('\n'):
                if self._mixin_phrase not in line:
                    continue
                for refid in re.findall(r'luadox:([0-9a-fA-F]+)', line):
                    ref = self.parser.refs_by_id.get(refid)
                    if isinstance(ref, ClassRef):
                        names.append(ref.name)
        return names

    def _class_parents(self, topref: ClassRef) -> List[str]:
        """
        Returns the LuaLS parent classes for a class, de-duplicated in order:

        * its @inherits superclass;
        * an optional mixin class <name><mixin_suffix> when configured and present
          (models e.g. Kanzi's createClass(Foo, super, FooMetadata), where FooMetadata
          carries the property and message types accessed as Foo.Member); and
        * any classes named in the configured mixin_doc_phrase (see _doc_mixins), which
          captures further mixins such as Kanzi "concepts".
        """
        parents: List[str] = []

        def add(name: Optional[str]) -> None:
            if name and name != topref.name and name not in parents:
                parents.append(name)

        add(topref.flags.get('inherits'))
        if self._mixin_suffix:
            candidate = topref.name + self._mixin_suffix
            if candidate in self._classnames:
                add(candidate)
        for name in self._doc_mixins(topref):
            if name in self._classnames:
                add(name)
        return parents

    def _emit_class(self, out: Callable[[str], None], topref: ClassRef) -> None:
        self.ctx.update(ref=topref)
        self._emit_doc(out, self._content_to_lines(topref.content))
        decl = '---@class {}'.format(topref.name)
        parents = self._class_parents(topref)
        if parents:
            decl += ' : {}'.format(', '.join(parents))
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
        # Optional [lua] config: a mixin suffix (see _class_parents) and a set of
        # globals the host injects into the script environment, as `name:type` tokens
        # (e.g. `globals = contextNode:Node`).
        self._classnames = {t.name for t in toprefs if isinstance(t, ClassRef)}
        self._mixin_suffix = self.config.get('luals', 'mixin_suffix', fallback='') or ''
        self._mixin_phrase = self.config.get('luals', 'mixin_doc_phrase', fallback='') or ''
        env_globals: List[Tuple[str, str]] = []
        for tok in files_str_to_list(self.config.get('luals', 'globals', fallback='')):
            name, _, typ = tok.partition(':')
            # A malformed name would be emitted as an assignment target and make
            # the whole definitions file syntactically invalid, so reject the
            # token instead.
            if not re.fullmatch(r'[A-Za-z_]\w*(\.[A-Za-z_]\w*)*', name):
                log.error('invalid [lua] globals token "%s": expected name[:type]', tok)
                continue
            env_globals.append((name, typ or 'any'))

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

        if env_globals:
            # Config globals have no source location and resolve globally, so clear the
            # parse context to keep a stale file:line off any diagnostic they produce.
            self.ctx.update(ref=None, file=None, line=None)
            out('-- Globals injected into the script execution environment.')
            for name, typ in env_globals:
                out('---@type {}'.format(self._map_type([typ])))
                out('{} = nil'.format(name))
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

        # Some members live under a namespace table (e.g. `gfx.Foo = nil`) whose root
        # is never declared on its own, which the language server flags as an undefined
        # global.  Declare a stub table for any such root.
        declared = set()
        used_roots = set()
        for line in lines:
            m = re.match(r'([A-Za-z_]\w*) = ', line) or re.match(r'function ([A-Za-z_]\w*)\(', line)
            if m:
                declared.add(m.group(1))
            m = re.match(r'(?:function )?([A-Za-z_]\w*)[.:]', line)
            if m:
                used_roots.add(m.group(1))
        stubs = sorted(used_roots - declared)
        if stubs:
            insert = ['-- Namespace tables for members declared under them.']
            insert += ['{} = {{}}'.format(name) for name in stubs]
            insert.append('')
            # Place stubs right after the header block (first blank line).
            pos = lines.index('') + 1 if '' in lines else len(lines)
            lines[pos:pos] = insert

        outfile = self._get_outfile(dst)
        with open(outfile, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines).rstrip() + '\n')
