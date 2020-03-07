# -*- coding: utf-8 -*-
"""
This module contains the Transducer and CompositeTransducer classes
which are responsible for performing transductions in the g2p library.
"""

import re
import copy
from typing import Dict, List, Pattern, Tuple, Union
from collections import defaultdict, OrderedDict
from collections.abc import Iterable
from g2p.mappings import Mapping
from g2p.mappings.utils import create_fixed_width_lookbehind, normalize
from g2p.exceptions import MalformedMapping
from g2p.log import LOGGER
from g2p.transducer.indices import Indices, IndexSequence

# Avoid TypeError in Python < 3.7 (see
# https://stackoverflow.com/questions/6279305/typeerror-cannot-deepcopy-this-pattern-object)
copy._deepcopy_dispatch[type(re.compile(''))] = lambda r, _: r

# An Index is typed as follows:
# {input_index: int, {'input_string': str, 'output': {output_index: int, str}}}
# Example:
# {0: {'input_string': 'h', 'output': {0: 'ʔ'}}}
Index = Dict

# A ChangeLog is a list of changes (List[int])
# The first item (int) in a change is the index of where the change occurs, and the second item (int) is the change offset
# Example:
# an insertion of length 1 at index 0 followed by a deletion of length one at index 2
# [[0,1],[2,-1]]
ChangeLog = List[List[int]]


class Transducer():
    """This is the fundamental class for performing conversions in the g2p library.

    Each Transducer must be initialized with a Mapping object. The Transducer object can then be called to apply the rules from Mapping on a given input.

    Attributes:
        mapping (Mapping): Formatted input/output pairs using the g2p.mappings.Mapping class.

    """

    def __init__(self, mapping: Mapping):
        self.mapping = mapping
        self.case_sensitive = mapping.kwargs['case_sensitive']
        self.norm_form = mapping.kwargs.get('norm_form', 'none')
        self.out_delimiter = mapping.kwargs.get('out_delimiter', '')
        self._index_match_pattern = re.compile(r'(?<={)\d+(?=})')
        self._char_match_pattern = re.compile(r'[^0-9\{\}]+(?={\d+})', re.U)
        self._output_change_log = defaultdict(int)

    def __repr__(self):
        return f"{__class__} between {self.mapping.kwargs.get('in_lang', 'und')} and {self.mapping.kwargs.get('out_lang', 'und')}"

    def __call__(self, to_convert: str, index: bool = False, debugger: bool = False):
        """The basic method to transduce an input. A proxy for self.apply_rules.

        Args:
            to_convert (str): The string to convert.
            index (bool, optional): Return indices in output. Defaults to False.
            debugger (bool, optional): Return intermediary steps for debugging. Defaults to False.

        Returns:
            Union[str, Tuple[str, Index], Tuple[str, List[dict]], Tuple[str, Index, List[dict]]]:
                Either returns a plain string (index=False, debugger=False),
                               a tuple with the converted string and indices (index=True, debugger=False),
                               a tuple with the converted string and corresponding rules (index=False, debugger=True),
                               a tuple with the converted string, indices and rules (index=True, debugger=True)
        """
        return self.apply_rules(to_convert, index, debugger)

    @staticmethod
    def _pua_to_index(string: str) -> int:
        """Given a string using with characters in the Supllementary Private Use Area A Unicode block
           Produce the number corresponding to the offset from the beginning of the block.

        Args:
            string (str): The string to convert

        Returns:
            int: The offset from the beginning of the block.
        """
        if string:
            intermediate_ord = ord(string[0])
            return intermediate_ord - 983040
        else:
            return - 1

    @staticmethod
    def edges_to_string(edges, input_nodes, output_nodes):
        edges = copy.deepcopy(edges)
        for i, edge in enumerate(edges):
            edges[i] = [input_nodes[edge[0]][1], output_nodes[edge[1]][1]]
        return edges

    def resolve_intermediate_chars(self, output_nodes):
        ''' Go through all nodes and resolve any intermediate characters from the Private Supplementary Use Area
            to their mapped equivalents.
        '''
        output_nodes = copy.deepcopy(output_nodes)
        indices_seen = defaultdict(int)
        for i, node in enumerate(output_nodes):
            intermediate_index = self._pua_to_index(node[1])
            # if not Private Supplementary Use character
            if intermediate_index < 0:
                continue
            else:
                output_char_index = indices_seen[intermediate_index]
                try:
                    output_nodes[i][1] = self.mapping[intermediate_index]['out'][output_char_index]
                except IndexError:
                    indices_seen[intermediate_index] = 0
                    output_char_index = 0
                    output_nodes[i][1] = self.mapping[intermediate_index]['out'][output_char_index]
                indices_seen[intermediate_index] += 1
        return output_nodes

    def update_explicit_indices(self, output_nodes, match, out_string: str, io, intermediate_diff, existing_edges) -> Index:
        """ Takes an arbitrary number of input & output strings and their corresponding index offsets.
            It then zips them up according to the provided indexing notation.

            Example:
                A rule that turns a sequence of k\u0313 to 'k might would have a default indexing of k -> ' and \u0313 -> k
                It might be desired though to show that k -> k and \u0313 -> ' and their indices were transposed.
                For this, the Mapping could be given the following: [{'in': 'k{1}\u0313{2}', 'out': "'{2}k{1}"}]
                Indices are found with r'(?<={)\d+(?=})' and characters are found with r'[^0-9\{\}]+(?={\d+})'

        Args:


        Returns:

        """
        input_char_matches = [x.group()
                              for x in self._char_match_pattern.finditer(io['in'])]
        input_match_indices = [
            x.group() for x in self._index_match_pattern.finditer(io['in'])]
        inputs = {}
        index = 0
        start = match.start() + intermediate_diff
        for i, m in enumerate(input_match_indices):
            for j, char in enumerate(input_char_matches[i]):
                if m in inputs:
                    inputs[m].append({'index': index + start, 'string': char})
                else:
                    inputs[m] = [{'index': index + start, 'string': char}]
                index += 1
        output_char_matches = [
            x.group() for x in self._char_match_pattern.finditer(out_string)]
        output_match_indices = [
            x.group() for x in self._index_match_pattern.finditer(out_string)]
        outputs = {}
        index = 0
        for i, m in enumerate(output_match_indices):
            for j, char in enumerate(output_char_matches[i]):
                if m in outputs:
                    outputs[m].append({'index': index + start, 'string': char})
                else:
                    outputs[m] = [{'index': index + start, 'string': char}]
                index += 1
        out_string = re.sub(re.compile(r'{\d+}'), '', out_string)
        output_nodes, new_edges = self.update_default_indices(
            output_nodes, match, out_string, intermediate_diff, existing_edges)
        edges = []
        for match_index, input_matches in inputs.items():
            output_matches = outputs[match_index]
            if len(input_matches) > len(output_matches):
                longest = input_matches
            else:
                longest = output_matches
            for i, item in enumerate(longest):
                if len(output_matches) > len(input_matches) and i > len(input_matches) - 1:
                    in_char = input_matches[-1]['index']
                    out_char = output_matches[i]['index']
                elif len(output_matches) < len(input_matches) and i > len(output_matches) - 1:
                    in_char = input_matches[i]['index']
                    out_char = output_matches[-1]['index']
                else:
                    in_char = input_matches[i]['index']
                    out_char = output_matches[i]['index']
                if out_char > len(match.group()) - 1 + start:
                    process = 'insert'
                elif out_char == len(out_string) - 1 + start and out_char < len(match.group()) - 1 + start:
                    process = 'delete'
                else:
                    process = 'explicit'
                edges.append((in_char, out_char, process))
        return output_nodes, edges

    def update_default_indices(self, output_nodes, match, out_string: str, intermediate_diff, edges):
        output_nodes = copy.deepcopy(output_nodes)
        start = match.start() + intermediate_diff
        in_string = match.group()
        in_length = len(in_string)
        out_length = len(out_string)
        change_log = []
        if in_length == out_length:
            for i, char in enumerate(out_string):
                output_nodes[i + start][1] = char
            return output_nodes, change_log
        # default insertion(s)
        elif in_length < out_length:
            longest = out_string
            shortest = in_string
            process = 'insert'
        # default deletion(s)
        else:
            longest = in_string
            shortest = out_string
            process = 'delete'
        # iterate the longest string
        for i, char in enumerate(longest):
            # if the shorter string still has that output, keep that index
            if i <= len(shortest) - 1:
                output_nodes[i + start][1] = out_string[i]
            # otherwise...
            else:
                # add a new node and increment each following node
                # log the change in order to update the edges.
                if process == 'insert':
                    unaffected_nodes = output_nodes[:i + start]
                    new_node = [i+start, char]
                    temp_inp = in_length - 1 + start
                    input_index = max([x[0] for x in edges if x[1] == temp_inp])
                    change_log.append(
                        (input_index, i + start, process))
                    changed_nodes = [[x[0] + 1, x[1]]
                                     for x in output_nodes[i+start:]]
                    output_nodes = unaffected_nodes + \
                        [new_node] + changed_nodes
                # delete the node and decrement each following node
                # log the change in order to update the edges.
                else:
                    # breakpoint()
                    try:
                        del output_nodes[i+start]
                    except IndexError:
                        if output_nodes:
                            del output_nodes[-1]
                        else:
                            continue
                    unaffected_nodes = output_nodes[:i + start]
                    temp_inp = start + i
                    input_index = max([x[0] for x in edges if x[1] == temp_inp])
                    change_log.append(
                        (input_index, temp_inp, process))
                    changed_nodes = [[x[0] - 1, x[1]]
                                     for x in output_nodes[i+start:]]
                    output_nodes = unaffected_nodes + changed_nodes
        return output_nodes, change_log

    def update_edges(self, existing_edges, new_edges):
        edges = copy.deepcopy(existing_edges)
        to_append = []
        # filter deletions before incrementing other edges
        edges = [x for x in edges if x not in [[y[0], y[1]] for y in new_edges if y[2] == 'delete']]
        for edge in new_edges:
            # If an edge is added, increment every following edge by one and append the edge
            if edge[2] == 'insert':
                for i in range(0, len(edges)):
                    if edges[i][1] >= edge[1]:
                        edges[i][1] += 1
                to_append.append([edge[0], edge[1]])
            # Else if an edge is removed, delete it and decrement every following edge by one
            elif edge[2] == 'delete':
                for i in range(0, len(edges)):
                    if edges[i][1] > edge[1]:
                        edges[i][1] -= 1
            elif edge[2] == 'explicit':
                for i in range(0, len(edges)):
                    if i >= len(edges):
                        break
                    if edges[i][0] == edge[0] or edges[i][1] == edge[1]:
                        del edges[i]
                to_append.append([edge[0], edge[1]])
        edges += to_append
        return sorted(edges, key=lambda x: x[0])

    def apply_rules(self, to_convert: str, index: bool, debugger: bool):
        # perform any normalization
        if not self.case_sensitive:
            to_convert = to_convert.lower()
        if self.norm_form:
            to_convert = normalize(to_convert, self.norm_form)
        # initialize values
        input_nodes = [[i, x] for i, x in enumerate(to_convert)]
        output_nodes = input_nodes
        edges = [[i, i] for i, x in enumerate(to_convert)]
        converted = to_convert
        rules_applied = []
        intermediate_forms = False
        self._output_change_log = defaultdict(int)
        # iterate rules
        for io in self.mapping:
            # Do not allow empty rules
            if not io['in'] and not io['out']:
                continue
            io = copy.deepcopy(io)
            intermediate_diff = 0
            for match in io['match_pattern'].finditer(converted):
                start = match.start() + intermediate_diff
                end = match.end() + intermediate_diff
                if 'intermediate_form' in io:
                    out_string = io['intermediate_form']
                    intermediate_forms = True
                else:
                    out_string = io['out']
                if self.out_delimiter:
                    # if not end segment, add delimiter
                    if not end >= len(converted):
                        out_string += self.out_delimiter
                if any(self._char_match_pattern.finditer(io['in'])) and any(self._char_match_pattern.finditer(out_string)):
                    new_nodes, new_edges = self.update_explicit_indices(
                        output_nodes, match, out_string, io, intermediate_diff, edges)
                else:
                    new_nodes, new_edges = self.update_default_indices(
                        output_nodes, match, out_string, intermediate_diff, edges)
                if debugger and new_nodes != output_nodes:
                    rules_applied.append({'input': converted,
                                          'output': ''.join([x[1] for x in new_nodes]),
                                          'rule': {k: v for k, v in io.items() if k != 'match_pattern'},
                                          'start': start,
                                          'end': end})
                intermediate_diff += (len(new_nodes) - len(output_nodes))
                output_nodes = new_nodes
                edges = self.update_edges(edges, new_edges)
                converted = ''.join([x[1] for x in output_nodes])
        edges = list(dict.fromkeys([tuple(x) for x in edges]))
        if intermediate_forms:
            output_nodes = self.resolve_intermediate_chars(output_nodes)
            converted = ''.join([x[1] for x in output_nodes])
        if index and debugger:
            rules_applied.append(self.edges_to_string(edges, input_nodes, output_nodes))
            return (converted, edges, rules_applied)
        if debugger:
            return (converted, rules_applied)
        if index:
            return (converted, edges)
        return converted


class Transducer1():
    """This is the fundamental class for performing conversions in the g2p library.

    Each Transducer must be initialized with a Mapping object. The Transducer object can then be called to apply the rules from Mapping on a given input.

    Attributes:
        mapping (Mapping): Formatted input/output pairs using the g2p.mappings.Mapping class.

    """

    def __init__(self, mapping: Mapping):
        self.mapping = mapping
        self.case_sensitive = mapping.kwargs['case_sensitive']
        self.norm_form = mapping.kwargs.get('norm_form', 'none')
        self.out_delimiter = mapping.kwargs.get('out_delimiter', '')
        self._index_match_pattern = re.compile(r'(?<={)\d+(?=})')
        self._char_match_pattern = re.compile(r'[^0-9\{\}]+(?={\d+})', re.U)

    def __repr__(self):
        return f"{__class__} between {self.mapping.kwargs.get('in_lang', 'und')} and {self.mapping.kwargs.get('out_lang', 'und')}"

    def __call__(self, to_convert: str, index: bool = False, debugger: bool = False):
        """The basic method to transduce an input. A proxy for self.apply_rules.

        Args:
            to_convert (str): The string to convert.
            index (bool, optional): Return indices in output. Defaults to False.
            debugger (bool, optional): Return intermediary steps for debugging. Defaults to False.

        Returns:
            Union[str, Tuple[str, Index], Tuple[str, List[dict]], Tuple[str, Index, List[dict]]]:
                Either returns a plain string (index=False, debugger=False),
                               a tuple with the converted string and indices (index=True, debugger=False),
                               a tuple with the converted string and corresponding rules (index=False, debugger=True),
                               a tuple with the converted string, indices and rules (index=True, debugger=True)
        """
        return self.apply_rules(to_convert, index, debugger)

    @staticmethod
    def _pua_to_index(string: str) -> int:
        """Given a string using with characters in the Supllementary Private Use Area A Unicode block
           Produce the number corresponding to the offset from the beginning of the block.

        Args:
            string (str): The string to convert

        Returns:
            int: The offset from the beginning of the block.
        """
        if string:
            intermediate_ord = ord(string[0])
            return intermediate_ord - 983040
        else:
            return - 1

    @staticmethod
    def get_offset_index(i: int, index_change_log: ChangeLog):
        """Given an index i, and a list of changes, determine the original index by undoing any changes that occured before it.

        Args:
            i: int
                index
            index_change_log: ChangeLog
                the list of previous changes to the indices

        Returns:
            The return value. True for success, False otherwise.
        """
        # breakpoint()
        index_change_log = copy.deepcopy(index_change_log)
        reversed_changes = [x for x in reversed(index_change_log)]
        for c_i, change in enumerate(reversed_changes):
            if change[0] < i:
                if abs(change[1]) == 1:
                    i -= change[1]
                elif abs(change[1]) == 0:
                    continue
                elif change[1] > 1 and i >= change[0] + change[1] - 1:
                    i -= change[1]
                elif change[1] < -1 and i >= change[0] + change[1] + 1:
                    i -= change[1]
                else:
                    i -= min(change[1], i - change[0] + 1)
                for n_i, next_change in enumerate(reversed_changes[c_i:]):
                    if next_change[0] >= change[0]:
                        reversed_changes[c_i + n_i][0] -= change[1]
        return i

    @staticmethod
    def return_incremented_indices(indices: Index, threshold: Tuple[int, int], start: int, diff: int) -> Index:
        """Given an Index, increment each output index by `diff` beginning at `start`
           except for indices whose input are between `threshold[0]` and `threshold[1]`.

        Args:
            indices (Index): The index to apply changes to.
            threshold (Tuple[int, int]): Input index range to not apply changes to.
            start (int): [description]: Output index where changes begin.
            diff (int): [description]: The change to apply.

        Returns:
            Index: Changed Index
        """
        indices = copy.deepcopy(indices)
        if diff != 0:
            # For outputs with more than one value
            # if increment, we reverse order the output keys
            # if decrement, we forward order the output keys
            if diff > 0:
                reverse = True
            else:
                reverse = False
            for k, v in indices.items():
                if k not in range(threshold[0], threshold[1]):
                    for k_o in sorted([x for x in v['output'].keys()], reverse=reverse):
                        if k_o >= start:
                            indices[k]['output'][k_o +
                                                 diff] = indices[k]['output'].pop(k_o)
        return indices

    @staticmethod
    def make_debugger_output_safe(debugger_output):
        new_output = []
        for x in debugger_output:
            if isinstance(x, dict):
                x['rule'] = {k: v for k, v in x['rule'].items()
                             if k != 'match_pattern'}
                new_output.append(x)
        return new_output

    @staticmethod
    def return_default_mapping(input_strings: List[str], output_strings: List[str],
                               input_index_offsets: List[int], output_index_offsets: List[int]) -> Index:
        """ Takes an arbitrary number of input & output strings and their corresponding index offsets.
            It then zips them up 1 by 1. If the input is longer than the output or vice versa, it continues zipping
            using the last item of either input or output respectively.

        Args:
            input_strings (List[str]): a list of input characters
            output_strings (List[str]): a list of output characters
            input_index_offsets (List[int]): a list of input character indices
            output_index_offsets (List[int]): a list of output character indices

        Returns:
            Index: returns an Index with the default mapping between inputs and outputs
        """

        new_input = {}
        # go through each input or output whichever is longer
        for i in range(0, max(len(input_strings), len(output_strings))):
            try:
                input_i = input_index_offsets[i]
            except IndexError:
                input_i = input_index_offsets[-1]
            try:
                output_i = output_index_offsets[i]
            except IndexError:
                output_i = output_index_offsets[-1]
            try:
                # if inputs and outputs are the same length, just zip them up
                new_input[input_i] = {'input_string': input_strings[i],
                                      'output': {output_i: output_strings[i]}}
            except IndexError:
                # but if the input is longer than output, use the last output character
                if len(input_strings) > len(output_strings):
                    new_input[input_i] = {'input_string': input_strings[i],
                                          'output': {output_i: output_strings[-1]}}
                # conversely if the output is longer than input, use the last input character
                elif len(input_strings) < len(output_strings):
                    if input_i in new_input:
                        intermediate_output = new_input[input_i]['output']
                    else:
                        intermediate_output = {}
                    new_input[input_i] = {'input_string': input_strings[-1],
                                          'output': {**intermediate_output, **{output_i: output_strings[i]}}}
        return new_input

    def return_expanded_format(self, input_string: str, output_string: str,
                               input_index: int, output_index: int) -> Tuple[List[str], List[str], List[int], List[int]]:
        """Given an input of length n and an output of length m as well as corresponding index offsets,
           return lists of lengths n & m corresponding to input characters/indices and output characters/indices respectively

        Args:
            input_string (str): an input string.
            output_string (str): an output string.
            input_index (int): the starting index of the input string.
            output_index (int): the starting index of the output string.

        Returns:
        Tuple(List[str], List[str], List[int], List[int]):
            Returns a quadruple consisting of a list of input characters,
                                              a list of output characters,
                                              an incremented list of corresponding input indices,
                                              an incremented list of corresponding output indices
        """

        # separate string into chars
        # add empty string if input/output is empty
        default_inputs = [x for x in input_string]
        if not default_inputs:
            default_inputs.append('')
        default_outputs = [x for x in output_string]
        if not default_outputs:
            default_outputs.append('')
        # get offsets for each char
        default_input_offsets = [
            i + input_index for i, v in enumerate(default_inputs)]
        default_output_offsets = [
            i + output_index for i, v in enumerate(default_outputs)]
        return (default_inputs, default_outputs, default_input_offsets, default_output_offsets)

    def explicit_indices(self, input_string: str, output_string: str, input_index: int, output_index: int) -> Index:
        """ Takes an arbitrary number of input & output strings and their corresponding index offsets.
            It then zips them up according to the provided indexing notation.

            Example:
                A rule that turns a sequence of k\u0313 to 'k might would have a default indexing of k -> ' and \u0313 -> k
                It might be desired though to show that k -> k and \u0313 -> ' and their indices were transposed.
                For this, the Mapping could be given the following: [{'in': 'k{1}\u0313{2}', 'out': "'{2}k{1}"}]
                Indices are found with r'(?<={)\d+(?=})' and characters are found with r'[^0-9\{\}]+(?={\d+})'

        Args:
            input_string (str): an input string.
            output_string (str): an output string.
            input_index (int): the starting index of the input string.
            output_index (int): the starting index of the output string.

        Returns:
            Index:
        """
        new_input = {}
        input_char_matches = [x.group()
                              for x in self._char_match_pattern.finditer(input_string)]

        input_match_indices = [
            x.group() for x in self._index_match_pattern.finditer(input_string)]
        inputs = [{'match_index': m, 'string': input_char_matches[i]}
                  for i, m in enumerate(input_match_indices)]
        output_char_matches = [x.group()
                               for x in self._char_match_pattern.finditer(output_string)]
        output_match_indices = [
            x.group() for x in self._index_match_pattern.finditer(output_string)]
        outputs = [{'match_index': m, 'string': output_char_matches[i]}
                   for i, m in enumerate(output_match_indices)]
        for match_index in input_match_indices:
            prev_input = ''
            # Get single character strings from inputs if they match the match_index
            explicit_inputs = []
            # Get offset for inputs by adding the length of the input string up to the match
            # plus the overall input index/offset
            explicit_input_offsets = []
            for v in inputs:
                if v['match_index'] == match_index:
                    for y_i, y_v in enumerate(v['string']):
                        explicit_inputs.append(y_v)
                        explicit_input_offsets.append(
                            len(prev_input) + input_index + y_i)
                prev_input += v['string']
            prev_output = ''
            # Get single character strings from outputs if they match the match_index
            explicit_outputs = []
            # Get offset for outputs by adding the length of the output string up to the match
            # plus the overall output index/offset
            explicit_output_offsets = []
            for v in outputs:
                if v['match_index'] == match_index:
                    for y_i, y_v in enumerate(v['string']):
                        explicit_outputs.append(y_v)
                        explicit_output_offsets.append(
                            len(prev_output) + output_index + y_i)
                prev_output += v['string']
            # Use default mapping to zip them up
            explicit_index = self.return_default_mapping(
                explicit_inputs, explicit_outputs, explicit_input_offsets, explicit_output_offsets)
            new_input = {**new_input, **explicit_index}
        return new_input

    def apply_rules(self, to_convert: str,
                    index: bool = False,
                    debugger: bool = False):
        """ Apply all the rules in self.mapping sequentially.
            Each rule in self.mapping is executed fully across the string (`to_convert`)
            before going to the next rules.
            Therefore, rules should be thought of as Phonological Rewrite Rules
            (https://en.wikipedia.org/wiki/Phonological_rule).
            Rules are also therefore susceptible to
            Bleeding/Feeding/CounterBleeding/CounterFeeding relationships
            (https://linguistics.stackexchange.com/questions/6084/whats-the-difference-between-counterbleeding-bleeding-and-feeding)

        Args:
            to_convert (str): The string to convert.
            index (bool, optional): Return indices in output. Defaults to False.
            debugger (bool, optional): Return intermediary steps for debugging. Defaults to False.

        Returns:
            Union[str, Tuple[str, Index], Tuple[str, List[dict]], Tuple[str, Index, List[dict]]]:
            Either returns:
                - a plain string (index=False, debugger=False),
                - a tuple with the converted string and indices (index=True, debugger=False),
                - a tuple with the converted string and corresponding rules (index=False, debugger=True),
                - a tuple with the converted string, indices and rules (index=True, debugger=True)
        """
        # Convert input as necessary
        if not self.case_sensitive:
            to_convert = to_convert.lower()

        if self.norm_form:
            to_convert = normalize(to_convert, self.norm_form)
        # Initialize
        indices = {}
        rules_applied = []
        converted = to_convert
        index_change_log = []
        intermediate = False
        for i, char in enumerate(converted):
            indices[i] = {'input_string': char, 'output': {i: char}}
        counter = 0
        # Go through each input/output pair in the provided Mapping object
        for io in self.mapping:
            if not io['in'] and not io['out']:
                continue
            # Make a copy of the input/output pair and reset the delimiter and intermediate diff
            io = copy.deepcopy(io)
            intermediate_diff = 0
            for match in io['match_pattern'].finditer(converted):
                counter += 1
                intermediate_to_convert = converted
                start = match.start() + intermediate_diff
                end = match.end() + intermediate_diff
                start_origin = self.get_offset_index(start, index_change_log)
                if 'intermediate_form' in io:
                    out_string = io['intermediate_form']
                    intermediate = True
                else:
                    out_string = io['out']
                # Add delimiter
                if self.out_delimiter:
                    # if not end segment, add delimiter
                    if not end >= len(converted):
                        out_string += self.out_delimiter

                # convert the final output
                output_sub = re.sub(
                    re.compile(r'{\d+}'), '', out_string)
                # We need to sub out the whole form because the match pattern could
                # include lookaheads and lookbehinds outside the match indices
                subbed = re.sub(io["match_pattern"], output_sub, converted)
                intermediate_form = converted[:start] + \
                    subbed[start:(start + len(out_string))] + converted[end:]
                if debugger and intermediate_form != converted:
                    applied_rule = {"input": converted,
                                    "rule": io, "output": intermediate_form,
                                    "start": start, "end": end}
                    rules_applied.append(applied_rule)
                # update intermediate converted form
                converted = intermediate_form

                # get the new index tuple
                if any(self._char_match_pattern.finditer(io['in'])) and any(self._char_match_pattern.finditer(out_string)):
                    new_index = self.explicit_indices(
                        io['in'], out_string, start_origin, start)
                else:
                    expanded = self.return_expanded_format(
                        match.group(), out_string, start_origin, start)
                    new_index = self.return_default_mapping(*expanded)
                to_delete = []
                to_merge = {}
                for k, v in new_index.items():
                    try:
                        if indices[k]['input_string'] != new_index[k]['input_string'] and len(intermediate_to_convert) - 1 >= k and new_index[k]['input_string'] == intermediate_to_convert[k]:
                            rebased_key = self.get_offset_index(
                                k, index_change_log)
                            if rebased_key in new_index.keys():
                                new_index[rebased_key]['output'].update(
                                    v['output'])
                            else:
                                to_merge = {
                                    **{rebased_key: {'output': v['output']}}, **to_merge}
                            if rebased_key != k:
                                to_delete.append(k)
                    except:
                        breakpoint()
                for k in to_delete:
                    del new_index[k]
                if to_merge:
                    new_index = {**to_merge, **new_index}
                index_difference = len(out_string) - len(io['in'])
                # # if it's not empty, then a rule has applied and it can be merged with the other indices
                # update
                for k, v in new_index.items():
                    new_output = v['output']
                    if len(v['output']) < len(indices[k]['output']) and index_difference >= 0:
                        indices[k]['output'].update(new_output)
                    else:
                        indices[k]['output'] = new_output
                inputs = new_index.keys()
                outputs = [x['output'].keys() for x in new_index.values()]
                values = {}
                for item in new_index.values():
                    for k, v in item['output'].items():
                        values[k] = v
                all_keys = [x for keys in outputs for x in keys]
                if index_difference != 0:
                    diff = 0
                    # check for (m)any to one:
                    dupes = {k: all_keys.count(k) for k in all_keys}
                    for k, v in dupes.items():
                        if v > 1:
                            diff = -(v - 1)
                            indices = self.return_incremented_indices(
                                indices, (min(inputs), max(inputs)+1), k, diff)
                            for item in index_change_log:
                                if item[0] >= k:
                                    item[0] += diff
                            index_change_log.append([k, diff])

                    # check for one to many:
                    one_to_many = [x for x in outputs if len(x) > 1]
                    for val in one_to_many:
                        min_out = min(val)
                        max_out = max(val)
                        diff = (max_out - min_out)
                        # threshold is equal to the first index where the change occurs
                        threshold = min(val) + 1
                        if diff:
                            indices = self.return_incremented_indices(
                                indices, (min(inputs), max(inputs)+1), threshold, diff)
                            for item in index_change_log:
                                if item[0] >= threshold:
                                    item[0] += diff
                            index_change_log.append([threshold, diff])
                    for pair in zip(inputs, outputs):
                        if len(pair[1]) > 1:
                            if min(pair[1]) < pair[0]:
                                val_in_index = self.get_offset_index(
                                    max(pair[1]), index_change_log)
                            else:
                                val_in_index = self.get_offset_index(
                                    min(pair[1]), index_change_log)
                            for k, v in indices.items():
                                if k > val_in_index:
                                    if min(indices[k]['output'].keys()) == min_out:
                                        try:
                                            popped = {min_out: indices[min(
                                                inputs)]['output'].pop(min_out)}
                                        except:
                                            breakpoint()
                                        bumped = indices[min(inputs)]['output']
                                        indices[min(inputs)]['output'] = popped
                                        indices[k]['output'] = bumped

                    # check deleted
                    if to_delete:
                        for k in to_delete:
                            diff += start - k
                        if diff > 0:
                            threshold = max(to_delete)
                        if diff < 0:
                            threshold = min(to_delete)
                        indices = self.return_incremented_indices(
                            indices, (min(new_index.keys()), max(new_index.keys()) + 1), threshold, diff)
                        for item in index_change_log:
                            if item[0] >= threshold:
                                item[0] += diff
                        index_change_log.append([threshold, diff])
                    intermediate_diff += diff
                # normalize
                for k, v in indices.items():
                    for k_o in v['output'].keys():
                        if k_o in all_keys:
                            indices[k]['output'][k_o] = values[k_o]
        if intermediate:
            indices_seen = defaultdict(int)
            for k, v in indices.items():
                for k_o, v_o in v['output'].items():
                    intermediate_index = self._pua_to_index(v_o)
                    if intermediate_index < 0:
                        continue
                    else:
                        output_char_index = indices_seen[intermediate_index]
                        try:
                            indices[k]['output'][k_o] = self.mapping[intermediate_index]['out'][output_char_index]
                        except IndexError:
                            indices_seen[intermediate_index] = 0
                            output_char_index = indices_seen[intermediate_index]
                            indices[k]['output'][k_o] = self.mapping[intermediate_index]['out'][output_char_index]
                        indices_seen[intermediate_index] += 1
        io_states = Indices(indices)
        if index and debugger:
            return (io_states.output(), io_states, rules_applied)
        if debugger:
            return (converted, rules_applied)
        if index:
            return (io_states.output(), io_states)
        return io_states.output()


class CompositeTransducer():
    """This class combines Transducer objects to form a CompositeTransducer object.

    Attributes:
        transducers (List[Transducer]): A list of Transducer objects to compose.
    """

    def __init__(self, transducers: List[Transducer]):
        self._transducers = transducers

    def __repr__(self):
        return f"{__class__} between {self._transducers[0].mapping.kwargs.get('in_lang', 'und')} and {self._transducers[-1].mapping.kwargs.get('out_lang', 'und')}"

    def __call__(self, to_convert: str, index: bool = False, debugger: bool = False):
        return self.apply_rules(to_convert, index, debugger)

    def apply_rules(self, to_convert: str, index: bool = False, debugger: bool = False):
        converted = to_convert
        indexed = []
        debugged = []
        for transducer in self._transducers:
            response = transducer(converted, index, debugger)
            if index:
                indexed.append(response[1])
                if debugger:
                    debugged += response[2]
            elif debugger:
                debugged += response[1]
            if index or debugger:
                converted = response[0]
            else:
                converted = response
        if index and debugger:
            return (converted, indexed, debugged)
        if index:
            return (converted, indexed)
        if debugger:
            return (converted, debugged)
        return converted
