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

__all__ = ['Prerenderer']

from typing import Union, Tuple, List

from .log import log
from .reference import *
from .parse import Parser
from .utils import *

class Prerenderer:
    """
    The prerender stage populates the specific typed Reference fields needed for
    rendering.

    All references are resolved to markdown links (whose target is in the form
    luadox:<refid>), and tags (such as @tparam) are parsed and validated.
    """
    def __init__(self, parser: Parser):
        self.parser = parser
        self.config = parser.config
        self.ctx = parser.ctx

    def process(self) -> List[TopRef]:
        """
        Preprocesses all Reference objects created by the parser by handling all remaining
        tags within content docstrings, normalizing content to markdown, and returns a
        sorted list of toprefs for rendering.
        """
        toprefs: list[TopRef] = []
        for ref in self.parser.topsyms.values():
            if isinstance(ref, (ClassRef, ModuleRef)):
                self._do_classmod(ref)
            elif isinstance(ref, ManualRef):
                self._do_manual(ref)
            if isinstance(ref, ClassRef):
                # An @inherits parent that resolves to no documented class leaves the
                # class's inheritance incomplete; report it through the diagnostics
                # collector so it feeds the exit code rather than being dropped silently.
                for name in ref.flags.get('inherits', []):
                    if not self.parser.refs.get(name):
                        self.parser.diagnostics.add(
                            'references',
                            '@inherits parent "{}" could not be resolved'.format(name),
                            ref.file, ref.line)
            toprefs.append(ref)
        toprefs.sort(key=lambda ref: (ref.type, ref.symbol))
        return toprefs

    def _apply_deprecated(self, ref: Reference) -> None:
        """
        Renders a @deprecated flag as a leading Deprecated admonition, so every
        renderer shows it without knowing about the flag.  The flag itself stays for
        renderers with a native deprecation representation.
        """
        if 'deprecated' not in ref.flags:
            return
        # The same postprocessor parse_raw_content() gives @warning bodies, so @{ref}
        # and `ref` in the explanation resolve to links.
        body = Content(postprocess=self.parser.refs_to_markdown)
        if ref.flags['deprecated']:
            body.md().append(ref.flags['deprecated'])
        ref.content.insert(0, deprecated_admonition(body))


    def _do_classmod(self, topref: Union[ClassRef, ModuleRef]) -> None:
        has_content = False
        for colref in self.parser.get_collections(topref):
            self.ctx.update(ref=colref)

            # Parse out section heading and body.
            _, _, content = self.parser.parse_raw_content(colref.raw_content)
            if isinstance(colref, (ClassRef, ModuleRef)):
                heading = colref.symbol
            else:
                heading = content.get_first_sentence(pop=True)
                # Fall back to section name if there is no content for the heading.
                heading = heading.strip() or colref.name

            colref.heading = heading
            colref.content = content
            self._apply_deprecated(colref)
            topref.collections.append(colref)

            functions = list(self.parser.get_elements_in_collection(FunctionRef, colref))
            fields = list(self.parser.get_elements_in_collection(FieldRef, colref))
            has_content = has_content or colref.content or functions or fields

            colref.compact = colref.flags.get('compact', [])
            fullnames: bool = colref.flags.get('fullnames', False)

            for ref in fields:
                self.ctx.update(ref=ref)
                _, _, content = self.parser.parse_raw_content(ref.raw_content)
                ref.title = ref.flags.get('display') or (ref.name if fullnames else ref.symbol)
                ref.types = ref.flags.get('type', [])
                ref.meta = ref.flags.get('meta')
                ref.content = content
                self._apply_deprecated(ref)
                colref.fields.append(ref)


            for ref in functions:
                self.ctx.update(ref=ref)
                paramsdict, returns, content = self.parser.parse_raw_content(ref.raw_content)
                # args is as defined in the function definition in source, while params is
                # based on tags.  Log a warning for any undocumented argument as long as
                # there is at least one documented parameter.
                params: List[Tuple[str, List[str], Content]] = []
                # ref.extra contains the list of parameter names as parsed from the
                # source.  Construct the params list based on 
                for param in ref.extra:
                    try:
                        params.append((param, *paramsdict[param]))
                    except KeyError:
                        params.append((param, [], Content()))
                        # Report regardless of whether any other parameter is documented:
                        # gating only partially documented functions would reward deleting
                        # the remaining @tparam lines.
                        self.parser.diagnostics.add(
                            'untyped',
                            '{}() missing @tparam for "{}" parameter'.format(ref.name, param),
                            ref.file, ref.line)

                ref.title = ref.display
                ref.params = params
                ref.returns = returns
                ref.meta = self.parser.refs_to_markdown(ref.flags['meta']) if 'meta' in ref.flags else ''
                ref.content = content
                self._apply_deprecated(ref)
                colref.functions.append(ref)

        topref.userdata['empty'] = not has_content


    def _do_manual(self, topref: ManualRef) -> None:
        if topref.raw_content:
            self.ctx.update(ref=topref)
            # Include any preamble before the first heading.
            _, _, content = self.parser.parse_raw_content(topref.raw_content)
            topref.content = content
            topref.heading = self.parser.refs_to_markdown(topref.heading)
        for ref in self.parser.get_collections(topref):
            # Manuals only have SectionRefs
            assert(isinstance(ref, SectionRef))
            self.ctx.update(ref=ref)
            _, _, content = self.parser.parse_raw_content(ref.raw_content)
            ref.heading = self.parser.refs_to_markdown(ref.heading)
            ref.content = content
            ref.level = int(ref.flags['level'])
            topref.collections.append(ref)

