// Copyright 2026 Christopher Barber
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// The iterative parse-tree traversal in this file is adapted from the ANTLR4
// runtime's IterativeParseTreeWalker, Copyright (c) 2012-2017 The ANTLR
// Project, licensed under the BSD-3-Clause license (see
// vendor/antlr4-cpp/LICENSE.txt).
//

// Bulk-event-stream collector: an iterative depth-first search over a finished
// parse tree that emits a flat (kind, payload, start, stop) int32 record per
// visited item into one contiguous buffer. Driven by the grammar-agnostic
// ParserInterpreter path in binding.cpp. Traversal mirrors
// IterativeParseTreeWalker (pre-order enter/terminal, post-order exit).
#pragma once

#include <cstdint>
#include <optional>
#include <utility>
#include <vector>

#include "ParserRuleContext.h"
#include "RuleContext.h"
#include "Token.h"
#include "tree/ErrorNode.h"
#include "tree/ParseTree.h"
#include "tree/TerminalNode.h"

namespace antlrope_events {

enum EventKind : int32_t {
    EV_ENTER_RULE = 0,
    EV_EXIT_RULE = 1,
    EV_TERMINAL = 2,
    EV_ERROR = 3,
};

// Character (codepoint) span of a rule, taken from its first/last tokens.
// Empty rules (no consumed token) yield -1. These offsets index the source the
// same way terminal offsets do, so Python can turn them into line/column.
inline void
rule_span(antlr4::ParserRuleContext *ctx, int32_t &start, int32_t &stop) {
    antlr4::Token *st = ctx->getStart();
    antlr4::Token *sp = ctx->getStop();
    start = st != nullptr ? static_cast<int32_t>(st->getStartIndex()) : -1;
    stop = sp != nullptr ? static_cast<int32_t>(sp->getStopIndex()) : -1;
}

// Build a keep-mask of size n from a Python index list. Callers pass nullptr
// (not this table) when a mask is absent, which collect_events reads as
// "keep all".
inline std::vector<char>
make_mask(const std::optional<std::vector<int32_t>> &idx, size_t n) {
    std::vector<char> keep(n, 0);
    for (int32_t i : *idx) {
        if (i >= 0 && static_cast<size_t>(i) < n)
            keep[static_cast<size_t>(i)] = 1;
    }
    return keep;
}

// Traverse the finished parse tree depth-first (pre-order rule-enter and
// terminals, post-order rule-exit) and append one (kind, payload, start, stop)
// int32 record per visited item to `out`. `rule_keep` / `tok_keep` are keep
// masks indexed by rule index / token type (nullptr = keep all); error nodes
// always survive the token mask.
inline void collect_events(antlr4::tree::ParseTree *root,
                           std::vector<int32_t> &out,
                           const char *rule_keep,
                           size_t n_rules,
                           const char *tok_keep,
                           size_t n_toks) {
    using namespace antlr4;
    std::vector<std::pair<tree::ParseTree *, size_t>> stack;
    tree::ParseTree *node = root;
    size_t index = 0;

    auto push =
        [&](int32_t kind, int32_t payload, int32_t start, int32_t stop) {
            out.push_back(kind);
            out.push_back(payload);
            out.push_back(start);
            out.push_back(stop);
        };

    while (node != nullptr) {
        // pre-order
        if (tree::TerminalNode::is(*node)) {
            Token *sym = static_cast<tree::TerminalNode *>(node)->getSymbol();
            size_t type = sym->getType();
            bool is_err = tree::ErrorNode::is(*node);
            // Errors always survive the token mask, so that filtering never
            // silently hides a parse failure.
            bool keep = is_err || tok_keep == nullptr ||
                        (type < n_toks && tok_keep[type]);
            if (keep) {
                push(is_err ? EV_ERROR : EV_TERMINAL,
                     static_cast<int32_t>(type),
                     static_cast<int32_t>(sym->getStartIndex()),
                     static_cast<int32_t>(sym->getStopIndex()));
            }
        } else {
            auto *ctx = static_cast<ParserRuleContext *>(node);
            size_t ridx = ctx->getRuleIndex();
            if (rule_keep == nullptr || (ridx < n_rules && rule_keep[ridx])) {
                int32_t start, stop;
                rule_span(ctx, start, stop);
                push(EV_ENTER_RULE, static_cast<int32_t>(ridx), start, stop);
            }
        }

        if (!node->children.empty()) {
            stack.emplace_back(node, index);
            index = 0;
            node = node->children[0];
            continue;
        }

        do {
            // post-order (rules only)
            if (!tree::TerminalNode::is(*node)) {
                auto *ctx = static_cast<ParserRuleContext *>(node);
                size_t ridx = ctx->getRuleIndex();
                if (rule_keep == nullptr ||
                    (ridx < n_rules && rule_keep[ridx])) {
                    int32_t start, stop;
                    rule_span(ctx, start, stop);
                    push(EV_EXIT_RULE, static_cast<int32_t>(ridx), start, stop);
                }
            }
            if (stack.empty()) {
                node = nullptr;
                index = 0;
                break;
            }
            if (stack.back().first->children.size() > ++index) {
                node = stack.back().first->children[index];
                break;
            }
            std::tie(node, index) = stack.back();
            stack.pop_back();
        } while (node != nullptr);
    }
}

// Collect the (rule_index, start, stop) character span of each parse-tree rule
// node whose index is kept by rule_keep (nullptr = all rules). With outermost =
// true, the traversal does not descend into a matched rule's subtree, so only
// top-level occurrences are emitted. This is the building block for rule-based
// chunking.
// Iterative (like collect_events) to avoid deep recursion on tall trees.
inline void collect_rule_spans(antlr4::tree::ParseTree *root,
                               std::vector<int32_t> &out,
                               const char *rule_keep,
                               size_t n_rules,
                               bool outermost) {
    using namespace antlr4;
    std::vector<std::pair<tree::ParseTree *, size_t>>
        stack;  // (node, child index)
    tree::ParseTree *node = root;

    while (node != nullptr) {
        bool descend = !node->children.empty();
        if (!tree::TerminalNode::is(*node)) {
            auto *ctx = static_cast<ParserRuleContext *>(node);
            size_t ridx = ctx->getRuleIndex();
            if (rule_keep == nullptr || (ridx < n_rules && rule_keep[ridx])) {
                int32_t start, stop;
                rule_span(ctx, start, stop);
                out.push_back(static_cast<int32_t>(ridx));
                out.push_back(start);
                out.push_back(stop);
                if (outermost) {
                    descend = false;  // skip the matched rule's subtree
                }
            }
        }

        if (descend) {
            stack.emplace_back(node, 0);
            node = node->children[0];
            continue;
        }

        // Ascend to the next unvisited sibling, unwinding exhausted parents.
        node = nullptr;
        while (!stack.empty()) {
            auto &top = stack.back();
            if (++top.second < top.first->children.size()) {
                node = top.first->children[top.second];
                break;
            }
            stack.pop_back();
        }
    }
}

}  // namespace antlrope_events
