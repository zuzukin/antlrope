# Installation

## Install the package

=== "pip"

    ```sh
    pip install antlrope
    ```

=== "conda"

    ```sh
    conda install -c conda-forge antlrope
    ```

=== "mamba"

    ```sh
    mamba install -c conda-forge antlrope
    ```

=== "pixi"

    ```sh
    pixi workspace channel add conda-forge
    pixi add antlrope
    ```

**Antlrope** depends on the official `antlr4-python3-runtime`, which your generated
parser modules import, so it is installed automatically.

Antlrope ships as a pre-compiled binary wheel with the C++ engine built in, so
nothing is compiled at install time. Each platform has a single CPython Stable ABI
(`abi3`) wheel that covers CPython 3.12 and newer. Wheels are published for:

- Linux ([manylinux], x86-64 and aarch64)
- macOS 11+ (Apple Silicon and Intel)
- Windows (x86-64)

The minimum supported Python version is 3.12.

## Install the ANTLR tool (to generate parsers)

To turn a `.g4` grammar into the Python parser modules Antlrope drives, you
also need the ANTLR tool itself, which is a Java program. The easiest way to get it
is the `antlr4-tools` package. It provides the `antlr4` command and downloads the
ANTLR jar (and, on first use, a JDK) for you:

=== "pip"

    ```sh
    pip install antlr4-tools
    ```

=== "conda"

    ```sh
    conda install -c conda-forge antlr4-tools
    ```

=== "mamba"

    ```sh
    mamba install -c conda-forge antlr4-tools
    ```

=== "pixi"

    ```sh
    pixi workspace channel add conda-forge
    pixi add antlr4-tools
    ```

You need the ANTLR tool only to generate or regenerate parsers, not to run them.
If you already have Java and the ANTLR jar, you can use those instead; nothing here
is specific to Antlrope.

See [Getting started](getting-started.md) for a complete walkthrough, from
generating a parser to writing and running a listener.

## From source

Clone the repo from https://github.com/zuzukin/antlrope:

=== "HTTPS"

    ```sh
    git clone https://github.com/zuzukin/antlrope.git
    ```

=== "SSH"

    ```sh
    git clone git@github.com:zuzukin/antlrope.git
    ```

=== "GitHub CLI"

    ```sh
    gh repo clone zuzukin/antlrope
    ```

The repository builds with [pixi](https://pixi.sh): `pixi run build` compiles the
C++ extension and `pixi run test` runs the suite. The build needs a C++17 compiler,
CMake 3.21 or later, and Ninja, all of which the pixi `default` environment
provides (through its `dev` feature). See
[CONTRIBUTING.md](https://github.com/zuzukin/antlrope/blob/dev/CONTRIBUTING.md) for details.

[manylinux]: glossary.md#manylinux
