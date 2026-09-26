# Glossary

Short definitions of the parsing, runtime, and packaging terms that appear
throughout these docs.

## ATN {#atn}

**Augmented Transition Network**: the state machine that ANTLR compiles a grammar
into. It has one sub-network per rule and drives the lexer's and parser's decisions.
ANTLR emits it as a compact serialized ATN (an integer array) inside the generated
parser and lexer modules. **Antlrope** deserializes that array and runs it directly
with the official C++ runtime, which is why no C++ code is generated per grammar.
See [How it works](concepts.md).

## ATN interpreter {#atn-interpreter}

ANTLR's `ParserInterpreter` and `LexerInterpreter`: the runtime components that
execute the [ATN](#atn) directly to parse input, instead of running code generated
for one specific grammar. Antlrope drives the C++ runtime's interpreters from the
serialized ATN, so it works for any grammar with no code-generation step of its own.

## DFA {#dfa}

**Deterministic Finite Automaton**: the lookahead automaton that ANTLR builds and
caches while parsing, to make each decision fast. Its states are shared, mutable
data attached to the [ATN](#atn). The stock C++ runtime guards changes to every DFA
with one lock on the ATN, so concurrent parses that share a grammar wait on each
other. The vendored runtime gives each DFA its own write lock, which is what lets
parses with a shared spec scale across threads. It also makes DFA-edge reads
lock-free, which speeds up single-threaded parsing. See
[How it works](concepts.md#the-vendored-runtime-and-its-patches) and
[Parallel parsing](parallel-parsing.md).

## embedded action {#embedded-action}

Target-language code written inline in a grammar between `{` and `}`, such as
`{ count += 1; }`. ANTLR copies it into the generated parser to run during a
parse. Because Antlrope interprets the [ATN](#atn) rather than running
grammar-specific generated code, it cannot execute embedded actions, and grammars
that depend on them won't parse correctly. See
[Performance & limitations](performance.md).

## facade {#facade}

The `<Grammar>EventListener` class that `antlrope gen` generates from your parser.
You subclass it and override only the callbacks you care about. It holds references
to your lexer and parser, and it provides `walk`, `walk_parallel`, and the chunkers.
It mirrors the
stock ANTLR listener interface so consumer code looks familiar. See
[Getting started](getting-started.md).

## FFI {#ffi}

**Foreign Function Interface**: the boundary across which Python calls into native
(C or C++) code and back. Every crossing has overhead, so a parse-tree walk that
calls Python for each node pays that cost once per node. Antlrope instead crosses once per parse, handing
Python a single bulk event buffer. See [How it works](concepts.md).

## GIL {#gil}

**Global Interpreter Lock**: CPython's lock that lets only one thread run Python
bytecode at a time. Antlrope releases the GIL during the native parse, so parses on
different threads run at the same time. Dispatching events to your Python callbacks
still holds the GIL, so the speedup from [`walk_parallel`](parallel-parsing.md)
depends on how much of the work is parsing. On a free-threaded (no-GIL) CPython
build, the dispatch also runs in parallel.

## manylinux {#manylinux}

A packaging standard for Linux Python wheels that are portable across many
distributions. Antlrope's Linux wheels target a manylinux baseline, so
`pip install` fetches a working pre-compiled binary on most systems without a local
compiler. See [Installation](installation.md).

## parse tree {#parse-tree}

The tree of rule and token nodes a traditional parser builds to represent the
structure of the input; ANTLR's listeners and visitors walk it node by node.
Antlrope builds its parse tree in C++ and never creates a Python parse tree;
instead, it delivers the same information as a flat, ordered stream of events. Use
the official runtime when you need a tree you can keep and navigate freely. See [How it works](concepts.md).

## semantic predicate {#semantic-predicate}

A grammar condition written in target-language code as `{ ... }?`, such as
`{ version >= 3 }?`, that ANTLR evaluates during a parse to choose between
alternatives. Like [embedded actions](#embedded-action), Antlrope cannot evaluate
them, so predicate-dependent grammars won't parse correctly. See
[Performance & limitations](performance.md).

## token channel {#token-channel}

A numbered stream the lexer can route tokens to (with `-> channel(...)`), used to
keep tokens like whitespace and comments out of the parser's way while still
preserving them. The default channel carries ordinary tokens; the token-based
[chunkers](chunking.md) let you choose which channel to split on.
