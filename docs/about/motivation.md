# Motivation

ANTLR is an excellent parser generator supporting multiple programming languages,
and its Python target is convenient: you write a `.g4` grammar, generate Python,
and consume the parse with a listener or visitor. However, the generated parser runs
in pure Python, which is slow for large inputs. The official `antlr4-python3-runtime`
is typically an order of magnitude slower than the C++ runtime. Pairing a C++ parse
with a Python `ParseTreeListener` doesn't solve this, because the listener pays for a
[foreign-function crossing][FFI] at every tree node, and for documents with millions
of nodes that cost dominates everything else.

The usual alternative is to switch your whole toolchain to the C++ (or Java)
target: generate a C++ parser, compile it, and write your application logic in C++.
That is a large step, and you lose the convenience of staying in Python.

**Antlrope** takes a different approach. It drives the official ANTLR4 C++ runtime's
[ATN interpreter] directly from the [serialized ATN][ATN] that the stock
`-Dlanguage=Python3` ANTLR tool already emits, so no C++ code is generated per
grammar and you have nothing to compile. The parse runs in C++, and instead of one
Python call per node, a single, filtered stream of events crosses into Python in
one batch. You keep writing plain Python listeners but get C++ parsing speed:
typically 10–20× faster than the pure-Python runtime, and faster still when you
subscribe to only part of the grammar.

The goal is to make the fast option also the easy one: if you can write an ANTLR
grammar and a Python class, you shouldn't have to learn C++ or change your build to
parse quickly.

## The name

*antl**rope*** is a pun on *antelope* that extends ANTLR's antler imagery. The
**ope** stands for **O**rdered **P**arse **E**vents, which is what sets this runtime
apart: it hands Python the parse as a single stream of events in depth-first order
(rule enter, rule exit, terminal, error) instead of a per-node parse-tree walk.

## When the official runtime is the right choice

Antlrope does not replace `antlr4-python3-runtime`; it is designed for
high-throughput parsing. Prefer the official runtime when your grammar relies on
[semantic predicates][semantic predicate] or [embedded actions][embedded action],
when you need to keep the [parse tree] (for random access, rewriting, or
re-walking) rather than make a single streaming pass, or when the input is small
enough that per-node cost doesn't matter. See [Migrating](../migrating.md) and
[Performance & limitations](../performance.md) for details.

[FFI]: ../glossary.md#ffi
[ATN interpreter]: ../glossary.md#atn-interpreter
[ATN]: ../glossary.md#atn
[semantic predicate]: ../glossary.md#semantic-predicate
[embedded action]: ../glossary.md#embedded-action
[parse tree]: ../glossary.md#parse-tree
