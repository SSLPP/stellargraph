# -*- coding: utf-8 -*-
#
# Copyright 2018-2020 Data61, CSIRO
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import argparse
import contextlib
import difflib
import enum
import glob
import itertools
import json
import nbformat
import os.path
import re
import subprocess
import sys
import textwrap

HTML_INDENT = 2
LINK_DEFAULT_TEXT = "demo"
TRUE_TEXT = "yes"
DOC_URL_BASE = "https://stellargraph.readthedocs.io/en/stable"
AUTOGENERATED_PROMPT = (
    f"autogenerated by {__file__}, edit that file instead of this location"
)
DOCS_LINK_SEPARATOR = "\n<!-- DOCS LINKS -->\n"


class LinkKind(enum.Enum):
    index = 1
    notebook = 2


class HtmlBuilder:
    def __init__(self, indent=None):
        self.html = []
        self.indent_amount = indent
        self.indent_level = 0
        self.add_count = 0

    def add(self, data, one_line=False):
        self.add_count += 1
        if one_line:
            self.html[-1] += data
        else:
            if self.indent_amount:
                indent = " " * (self.indent_amount * self.indent_level)
                data = indent + data

            self.html.append(data)

    @contextlib.contextmanager
    def element(self, name, attrs={}, only_with_attrs=False):
        """Open (and automatically) close an HTML element"""
        if only_with_attrs and not attrs:
            yield
            return

        attrs_str = " ".join(f"{name}='{value}'" for name, value in attrs.items())
        if attrs_str:
            attrs_str = " " + attrs_str

        self.add(f"<{name}{attrs_str}>")
        self.indent_level += 1
        initial_len = len(self.html)
        try:
            yield
        finally:
            self.indent_level -= 1
            closing = f"</{name}>"
            self.add(f"</{name}>", one_line=len(self.html) == initial_len)

    def string(self):
        sep = "" if self.indent_amount is None else "\n"
        return sep.join(self.html)


class T:
    def __init__(self, text=None, link=None, details=None, kind=LinkKind.notebook):
        if text is None:
            if link is None:
                raise ValueError("must specify at least one of 'text' and 'link'")

            text = LINK_DEFAULT_TEXT

        self.text = text
        self.link = link
        self.details = details
        self.kind = kind
        assert isinstance(kind, LinkKind)

    @staticmethod
    def textify(inp):
        if not inp:
            return None
        elif inp is True:
            return T(TRUE_TEXT)
        elif isinstance(inp, list):
            return [T.textify(x) for x in inp]
        elif isinstance(inp, T):
            return inp

        return T(inp)


class Format(abc.ABC):
    def __init__(self, file_name, separator):
        self.file_name = file_name
        self.separator = separator

    @abc.abstractmethod
    def render(self, headings, algorithms):
        ...

    @abc.abstractmethod
    def index_suffix(self, checking):
        ...

    @abc.abstractproperty
    def notebook_suffix(self, checking):
        ...

    def link(self, t, checking=False):
        if t.link is None:
            return None

        if t.kind is LinkKind.index:
            suffix = self.index_suffix(checking)
        elif t.kind is LinkKind.notebook:
            suffix = self.notebook_suffix(checking)
        return f"{t.link}{suffix}"


class Html(Format):
    def index_suffix(self, checking):
        return "/README.md"

    def notebook_suffix(self, checking):
        return ".ipynb"

    def render(self, headings, algorithms):
        builder = HtmlBuilder(indent=2)

        builder.add(f"<!-- {AUTOGENERATED_PROMPT} -->")
        with builder.element("table"):
            with builder.element("tr"):
                for heading in headings:
                    with builder.element("th"):
                        builder.add(self._render_t(heading), one_line=True)

            for algorithm in algorithms:
                with builder.element("tr"):
                    for heading in headings:
                        with builder.element("td"):
                            self._render_cell(builder, algorithm.columns[heading])

        return builder.string()

    def _render_t(self, t):
        html = HtmlBuilder()

        title_attr = {"title": t.details} if t.details else {}
        link = self.link(t)
        href_attr = {"href": link} if link else {}

        # add a span if we need details (hover) text, and an link if there's a link
        with html.element("span", title_attr, only_with_attrs=True):
            with html.element("a", href_attr, only_with_attrs=True):
                html.add(t.text)

        return html.string()

    def _render_cell(self, html, cell, one_line=True):
        if not cell:
            return

        if isinstance(cell, list):
            for contents in cell:
                # multiple elements? space them out
                self._render_cell(html, contents, one_line=False)
        else:
            html.add(self._render_t(cell), one_line=one_line)


class Rst(Format):
    def index_suffix(self, checking):
        if checking:
            # when checking links, we need to write out the exact filename
            return "/index.rst"
        return "/index"

    def notebook_suffix(self, checking):
        if checking:
            return ".nblink"
        return ""

    def render(self, headings, algorithms):
        result = [".. list-table::", "   :header-rows: 1", ""]

        new_row = "   *"
        new_item = "     -"

        result.append(new_row)
        for heading in headings:
            rst = self._render_t(heading)
            result.append(f"{new_item} {rst}")

        for algorithm in algorithms:
            result.append(new_row)
            for heading in headings:
                rst = self._render_cell(algorithm.columns[heading])
                if rst:
                    result.append(f"{new_item} {rst}")
                else:
                    result.append(new_item)

        return "\n".join(result)

    def _render_t(self, t):
        link = self.link(t)
        # RST doesn't support the title, directly, but CSS gives us a bit more control to display
        # longer column headings
        text = t.details if t.details else t.text
        if link:
            return f":any:`{text} <{link}>`"

        return text

    def _render_cell(self, cell):
        if not cell:
            return ""

        if isinstance(cell, list):
            return ", ".join(self._render_cell(contents) for contents in cell)
        else:
            return self._render_t(cell)


def find_links(element, fmt):
    # traverse over the collection(s) to find all the links in T's
    if element is None:
        pass
    elif isinstance(element, T):
        rendered_link = fmt.link(element, checking=True)
        if rendered_link:
            yield (element.link, rendered_link)
    elif isinstance(element, list):
        for sub in element:
            yield from find_links(sub, fmt)
    elif isinstance(element, Algorithm):
        for sub in element.columns.values():
            yield from find_links(sub, fmt)
    else:
        raise ValueError(f"unsupported element in link finding {element!r}")


def link_is_valid_relative(link, base_dir):
    if link is None:
        return True

    if os.path.isabs(link):
        # absolute links aren't allowed
        return False

    if link.lower().startswith("http"):
        # github (and other website) links aren't allowed either
        return False

    return os.path.exists(os.path.join(base_dir, link))


# Columns
def index_link(*args, **kwargs):
    return T(*args, **kwargs, kind=LinkKind.index)


ALGORITHM = T("Algorithm")
HETEROGENEOUS = T("Heter.", details="Heterogeneous")
DIRECTED = T("Dir.", details="Directed")
WEIGHTED = T("EW", details="Edge weights")
TEMPORAL = T("T", details="Time-varying, temporal")
FEATURES = T("NF", details="Node features")
NC = index_link("NC", link="node-classification", details="Node classification")
LP = index_link("LP", link="link-prediction", details="Link prediction")
RL = index_link("Unsup.", link="embeddings", details="Unsupervised")
INDUCTIVE = T("Ind.", details="Inductive")
GC = index_link("GC", link="graph-classification", details="Graph classification")

COLUMNS = [
    ALGORITHM,
    HETEROGENEOUS,
    DIRECTED,
    WEIGHTED,
    TEMPORAL,
    FEATURES,
    NC,
    LP,
    RL,
    INDUCTIVE,
    GC,
]


class Algorithm:
    def __init__(
        self,
        algorithm,
        *,
        heterogeneous=None,
        directed=None,
        weighted=None,
        temporal=None,
        features=None,
        nc=None,
        interpretability_nc=None,
        lp=None,
        rl=None,
        inductive=None,
        gc=None,
    ):
        columns = {
            ALGORITHM: algorithm,
            HETEROGENEOUS: heterogeneous,
            DIRECTED: directed,
            WEIGHTED: weighted,
            TEMPORAL: temporal,
            FEATURES: features,
            NC: nc,
            LP: lp,
            RL: rl,
            INDUCTIVE: inductive,
            GC: gc,
        }

        self.columns = {name: T.textify(value) for name, value in columns.items()}


HETEROGENEOUS_EDGE = T("yes, edges", details="multiple edges types")


def rl_us(link=None):
    return T("US", link=link, details="UnsupervisedSampler")


def rl_dgi(link="embeddings/deep-graph-infomax-embeddings"):
    return T("DGI", link=link, details="DeepGraphInfomax")


def via_rl(link=None):
    return T("via unsup.", link=link, details="via embedding vectors",)


ALGORITHMS = [
    Algorithm(
        T("GCN", details="Graph Convolutional Network (GCN)"),
        heterogeneous="see RGCN",
        features=True,
        temporal="see T-GCN",
        nc=T(link="node-classification/gcn-node-classification"),
        interpretability_nc=T(link="interpretability/gcn-node-link-importance"),
        lp=T(link="link-prediction/gcn-link-prediction"),
        rl=[rl_us(), rl_dgi()],
        inductive="see Cluster-GCN",
        gc=T(link="graph-classification/gcn-supervised-graph-classification"),
    ),
    Algorithm(
        "Cluster-GCN",
        features=True,
        nc=T(link="node-classification/cluster-gcn-node-classification"),
        lp=True,
        inductive=True,
    ),
    Algorithm(
        T("RGCN", details="Relational GCN (RGCN)"),
        heterogeneous=HETEROGENEOUS_EDGE,
        features=True,
        nc=T(link="node-classification/rgcn-node-classification"),
        lp=True,
    ),
    Algorithm(
        T("T-GCN", details="Temporal GCN (T-GCN), implemented as GCN-LSTM"),
        features="time series, sequence",
        temporal="node features",
        nc=T(link="time-series/gcn-lstm-time-series"),
    ),
    Algorithm(
        T("GAT", details="Graph ATtention Network (GAT)"),
        features=True,
        nc=T(link="node-classification/gat-node-classification"),
        interpretability_nc=T(link="interpretability/gat-node-link-importance"),
        lp=True,
        rl=[rl_us(), rl_dgi()],
    ),
    Algorithm(
        T("SGC", details="Simplified Graph Convolution (SGC)"),
        features=True,
        nc=T(link="node-classification/sgc-node-classification"),
        lp=True,
    ),
    Algorithm(
        T("PPNP", details="Personalized Propagation of Neural Predictions (PPNP)"),
        features=True,
        nc=T(link="node-classification/ppnp-node-classification"),
        lp=True,
        rl=[rl_us(), rl_dgi(link=None)],
    ),
    Algorithm(
        T("APPNP", details="Approximate PPNP (APPNP)"),
        features=True,
        nc=T(link="node-classification/ppnp-node-classification"),
        lp=True,
        rl=[rl_us(), rl_dgi()],
    ),
    Algorithm(
        "GraphWave",
        nc=via_rl(),
        lp=via_rl(),
        rl=T(link="embeddings/graphwave-embeddings"),
    ),
    Algorithm(
        "Attri2Vec",
        features=True,
        nc=T(link="node-classification/attri2vec-node-classification"),
        lp=T(link="link-prediction/attri2vec-link-prediction"),
        rl=T(link="embeddings/attri2vec-embeddings"),
    ),
    Algorithm(
        "GraphSAGE",
        heterogeneous="see HinSAGE",
        directed=T(link="node-classification/directed-graphsage-node-classification"),
        features=True,
        nc=T(link="node-classification/graphsage-node-classification"),
        lp=T(link="link-prediction/graphsage-link-prediction"),
        rl=[
            rl_us(link="embeddings/graphsage-unsupervised-sampler-embeddings"),
            rl_dgi(),
        ],
        inductive=T(link="node-classification/graphsage-inductive-node-classification"),
    ),
    Algorithm(
        "HinSAGE",
        heterogeneous=True,
        features=True,
        nc=True,
        lp=T(link="link-prediction/hinsage-link-prediction"),
        rl=rl_dgi(),
        inductive=True,
    ),
    Algorithm(
        "Node2Vec",
        weighted=T(link="node-classification/node2vec-weighted-node-classification"),
        nc=via_rl(link="node-classification/node2vec-node-classification"),
        lp=via_rl(link="link-prediction/node2vec-link-prediction"),
        rl=T(link="embeddings/node2vec-embeddings"),
    ),
    Algorithm(
        "Metapath2Vec",
        heterogeneous=True,
        nc=via_rl(),
        lp=via_rl(),
        rl=T(link="embeddings/metapath2vec-embeddings"),
    ),
    Algorithm(
        T("CTDNE", details="Continuous-Time Dynamic Network Embeddings"),
        temporal=True,
        nc=via_rl(),
        lp=via_rl(link="link-prediction/ctdne-link-prediction"),
        rl=True,
    ),
    Algorithm(
        "Watch Your Step",
        nc=via_rl(link="embeddings/watch-your-step-embeddings"),
        lp=via_rl(),
        rl=T(link="embeddings/watch-your-step-embeddings"),
    ),
    Algorithm(
        "ComplEx",
        heterogeneous=HETEROGENEOUS_EDGE,
        directed=True,
        nc=via_rl(),
        lp=T(link="link-prediction/complex-link-prediction"),
        rl=True,
    ),
    Algorithm(
        "DistMult",
        heterogeneous=HETEROGENEOUS_EDGE,
        directed=True,
        nc=via_rl(),
        lp=T(link="link-prediction/distmult-link-prediction"),
        rl=True,
    ),
    Algorithm(
        T("DGCNN", details="Deep Graph CNN"),
        features=True,
        gc=T(link="graph-classification/dgcnn-graph-classification"),
    ),
]


FILES = [
    # a RST comment is a directive with an unknown type, like an empty string
    Rst("docs/demos/index.rst", "\n..\n   DEMO TABLE MARKER\n"),
    Html("demos/README.md", "\n<!-- DEMO TABLE MARKER -->\n"),
]


def tables(action):
    compare = action == "compare"
    for file_fmt in FILES:
        new_table = file_fmt.render(COLUMNS, ALGORITHMS)
        file_name = file_fmt.file_name
        separator = file_fmt.separator

        base_dir = os.path.dirname(file_name)
        invalid_links = [
            (written, rendered)
            for written, rendered in itertools.chain(
                find_links(COLUMNS, file_fmt), find_links(ALGORITHMS, file_fmt)
            )
            if not link_is_valid_relative(rendered, base_dir)
        ]

        if invalid_links:
            formatted = "\n".join(
                f"- `{written}` (missing target: `{base_dir}/{rendered}`)"
                for written, rendered in invalid_links
            )
            error(
                f"expected all links in algorithm specifications in `{__file__}` to be relative links that are valid starting at `{base_dir}`, but found {len(invalid_links)} invalid:\n\n{formatted}",
                edit_fixit=True,
            )

        separate_compare_overwrite(
            file_name, separator, action=action, new_middle=new_table, label="table"
        )


TITLE_RE = re.compile("^# (.*)")


def demo_listing_table(root):
    repo_dir = os.getcwd()

    os.chdir(root)
    try:
        yield "| Demo | Source |"
        yield "|---|---|"
        # sort the demos to get a consistent order, independent of the file system traversal order
        for demo in sorted(glob.iglob("**/*.ipynb", recursive=True)):
            if ".ipynb_checkpoint" in demo:
                continue

            notebook = nbformat.read(demo, as_version=4)
            markdown = "".join(notebook.cells[0].source)
            title = TITLE_RE.match(markdown)
            text = title[1]

            demo_html = demo.replace(".ipynb", ".html")
            url = os.path.join(DOC_URL_BASE, root, demo_html)

            # this looks better if the two links are separated (hence ; and the explicit new line),
            # and the "open here" doesn't get split across lines (hence non-breaking space)
            yield f"| [{text}]({url}) | [source]({demo}) |"
    finally:
        os.chdir(repo_dir)


def demo_indexing(action):
    root_dir = "demos/"

    for directory in glob.iglob("demos/**/", recursive=True):
        readme = os.path.join(directory, "README.md")
        if not os.path.exists(readme):
            # FIXME(#1139): some demos directories don't have a README
            continue

        index = os.path.join("docs", directory, "index.txt")
        if not os.path.exists(index):
            error(
                f"expected each demo README to match a docs 'index.txt' file, found `{readme}` without corresponding `{index}`"
            )

        link = f"{DOC_URL_BASE}/{directory}"
        if directory != root_dir:
            # the root readme already has the detailed table in it, so don't include the full list
            # of demos there.

            listing = "\n".join(demo_listing_table(directory))
            suffix = "The demo titles link to the latest, nicely rendered version. The 'source' links will open the demo in the application in which this README is being viewed, such as Jupyter Lab (ready for execution)."
        else:
            listing = ""
            suffix = ""

        new_contents = f"""\
<!-- {AUTOGENERATED_PROMPT} -->

These demos are displayed with detailed descriptions in the documentation: {link}

{listing}

{suffix}"""

        separate_compare_overwrite(
            readme,
            DOCS_LINK_SEPARATOR,
            action=action,
            new_middle=new_contents,
            label="docs link",
        )


def separate_compare_overwrite(file_name, separator, action, new_middle, label):
    with open(file_name, "r+") as f:
        file_contents = f.read()
        parts = file_contents.split(separator)

        if len(parts) != 3:
            code_block = textwrap.indent(separator.strip(), "    ")
            error(
                f"expected exactly two instances of the separator on their own lines in `{file_name}`, found {len(parts) - 1} instances. Separator should be:\n\n{code_block}"
            )

        prefix, current_middle, suffix = parts
        if action == "compare" and new_middle != current_middle:

            diff = difflib.unified_diff(
                current_middle.splitlines(keepends=True),
                new_middle.splitlines(keepends=True),
                fromfile=file_name,
                tofile="autogenerated expected contents",
            )
            sys.stdout.writelines(diff)

            error(
                f"existing {label} in `{file_name}` differs to generated {label}; was it edited manually?",
                edit_fixit=True,
            )
        elif action == "overwrite":
            f.seek(0)
            f.write("".join([prefix, separator, new_middle, separator, suffix]))
            # delete any remaining content
            f.truncate()


def error(message, edit_fixit=False):
    formatted = f"Error while generating information for documentation: {message}"
    if edit_fixit:
        formatted += f"\n\nTo fix, edit `{__file__}` as appropriate and run it like `python {__file__} --action=overwrite` to overwrite existing information with updated form."

    print(formatted, file=sys.stderr)

    try:
        subprocess.run(
            [
                "buildkite-agent",
                "annotate",
                "--style=error",
                "--context=demo_indexing",
                formatted,
            ]
        )
    except FileNotFoundError:
        # no agent, so probably on buildkite, and so silently no annotation
        pass

    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Edits or compares the table of all algorithms and their demos in `demos/README.md` and `docs/demos/index.txt`"
    )
    parser.add_argument(
        "--action",
        choices=["compare", "overwrite"],
        default="compare",
        help="whether to compare the tables against what would be generated, or to overwrite them table with new ones (default: %(default)s)",
    )
    args = parser.parse_args()

    tables(args.action)
    demo_indexing(args.action)


if __name__ == "__main__":
    main()
