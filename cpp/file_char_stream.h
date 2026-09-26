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

// A streaming CharStream that reads a UTF-8 file incrementally for the lexer,
// without ever holding the whole input in memory.
//
// The vendored runtime's own streaming stream, UnbufferedCharStream, takes a
// std::wistream of pre-decoded wide characters and uses an in-band 0xFFFF EOF
// marker. Both depend on wchar_t, which is 16-bit (UTF-16) on Windows and would
// corrupt codepoints outside the Basic Multilingual Plane. So this is a
// standalone char32_t stream that owns the UTF-8 decode (via the runtime's own
// incremental antlrcpp::Utf8::decode) and a random-access window over the
// codepoints it has decoded.
//
// Unlike UnbufferedCharStream, the window is not auto-compacted on consume():
// the lexer over-consumes during prediction and then seek()s back (see
// LexerATNSimulator::accept), so the buffer is kept and only dropped explicitly
// via dropThrough() at chunk boundaries. The chunker keeps the window from the
// current chunk's start, which is at or before every lexer mark inside the
// chunk. That keeps the lexer's mark/seek lookahead valid and lets getText()
// read a whole chunk span (many tokens plus the whitespace between them) by
// absolute index. Peak memory is one chunk plus the lexer's bounded prediction
// overshoot.

#pragma once

#include <cstddef>
#include <fstream>
#include <string>
#include <string_view>

#include "CharStream.h"
#include "Exceptions.h"
#include "IntStream.h"
#include "misc/Interval.h"
#include "support/Utf8.h"

namespace antlrope {

class Utf8FileCharStream : public antlr4::CharStream {
public:
    // Opens `path` (UTF-8 bytes). `lenient` substitutes U+FFFD for malformed
    // input (strict throws). `block` is the byte read size (0 => 64 KiB; a
    // small value is a test hook to exercise multi-byte sequences split across
    // reads).
    explicit Utf8FileCharStream(const std::string &path,
                                bool lenient,
                                size_t block)
        : _name(path), _lenient(lenient), _block(block != 0 ? block : 65536) {
        _in.open(path, std::ios::binary);
        if (!_in) {
            throw std::runtime_error("cannot open file: " + path);
        }
    }

    // --- IntStream / CharStream interface ----------------------------------

    // Advance past LA(1). Throws (per the interface) if already at EOF.
    void consume() override {
        if (LA(1) == antlr4::IntStream::EOF) {
            throw antlr4::IllegalStateException("cannot consume EOF");
        }
        _pos++;
    }

    // Lookahead: the codepoint `i` positions ahead (LA(1) = next), decoding
    // more input on demand; EOF past the end. Negative `i` looks back into the
    // retained window (0 when the position was dropped).
    size_t LA(ssize_t i) override {
        if (i == 0) {
            return 0;  // undefined per IntStream; the lexer never calls LA(0).
        }
        if (i < 0) {
            // LA(-1) is the previously read char (abs index _pos - 1).
            size_t back = static_cast<size_t>(-i);
            if (back > _pos || _pos - back < _buf_start) {
                return 0;  // before the start of the (retained) stream
            }
            return _buf[_pos - back - _buf_start];
        }
        size_t abs = _pos + static_cast<size_t>(i) - 1;
        ensure_loaded(abs);
        if (abs >= _buf_start + _buf.size()) {
            return antlr4::IntStream::EOF;
        }
        return _buf[abs - _buf_start];
    }

    // mark() and release() are bookkeeping only: the window is never compacted
    // here (the chunker calls dropThrough() at boundaries), so the lexer's
    // lookahead region stays valid. That region is always inside the current
    // chunk, which the window already retains.
    ssize_t mark() override {
        _mark_depth++;
        return -static_cast<ssize_t>(_mark_depth);
    }

    void release(ssize_t marker) override {
        if (marker != -static_cast<ssize_t>(_mark_depth)) {
            throw antlr4::IllegalStateException(
                "release() called with an invalid marker.");
        }
        _mark_depth--;
    }

    // Absolute codepoint index of LA(1).
    size_t index() override { return _pos; }

    // Reposition within the retained window (the lexer seeks back after
    // prediction overshoot), decoding forward as needed; seeking past EOF
    // lands on the EOF position, seeking before the window throws.
    void seek(size_t index) override {
        ensure_loaded(index);
        size_t hi = _buf_start + _buf.size();
        if (index > hi) {
            index = hi;  // seeking past EOF lands on the EOF position
        }
        if (index < _buf_start) {
            throw antlr4::IllegalArgumentException(
                "cannot seek before the retained window");
        }
        _pos = index;
    }

    // Unsupported by design: a streaming source has no known total size, and
    // the only runtime path that asks for it is whole-input error recovery.
    size_t size() override {
        throw antlr4::UnsupportedOperationException(
            "streaming char source has no known total size: size() requires "
            "the whole input. This usually means the parser fell back to "
            "whole-input error recovery on a malformed record. Give "
            "stream_by_rule or stream_on_token a clean sequence of records, or "
            "use the in-memory chunkers (chunk_by_rule, split_*) for input "
            "that needs a full parse.");
    }

    // The file path, or ANTLR's unknown-source placeholder when empty.
    std::string getSourceName() const override {
        return _name.empty() ? antlr4::IntStream::UNKNOWN_SOURCE_NAME : _name;
    }

    // UTF-8 text for an inclusive absolute-index interval. The interval must
    // lie within the retained window (the chunkers only ask for spans they
    // have not yet dropped); throws if part of it was already freed.
    std::string getText(const antlr4::misc::Interval &interval) override {
        if (interval.a < 0 || interval.b < interval.a - 1) {
            throw antlr4::IllegalArgumentException("invalid interval");
        }
        if (interval.b < interval.a) {
            return std::string();  // empty interval
        }
        size_t a = static_cast<size_t>(interval.a);
        size_t b = static_cast<size_t>(interval.b);
        ensure_loaded(b);
        if (a < _buf_start || b >= _buf_start + _buf.size()) {
            throw antlr4::IllegalArgumentException(
                "getText interval outside the retained window");
        }
        return antlrcpp::Utf8::lenientEncode(
            std::u32string_view(_buf).substr(a - _buf_start, b - a + 1));
    }

    // Best-effort: the runtime occasionally stringifies the stream for
    // messages; return the current window rather than throw (it never needs the
    // whole input).
    std::string toString() const override {
        return antlrcpp::Utf8::lenientEncode(_buf);
    }

    // --- chunker-facing helpers --------------------------------------------

    // The decoded codepoints currently retained, starting at absolute index
    // bufStart(). The chunker scans this directly for whitespace trimming and
    // incremental line/column, and slices chunk text via getText().
    const std::u32string &buffer() const { return _buf; }
    // Absolute codepoint index of buffer()[0].
    size_t bufStart() const { return _buf_start; }

    // Drop everything before absolute codepoint `index`, freeing the emitted
    // prefix. `index` must be within [bufStart(), index()] (a passed boundary).
    void dropThrough(size_t index) {
        if (index <= _buf_start) {
            return;
        }
        size_t cut = index - _buf_start;
        if (cut > _buf.size()) {
            cut = _buf.size();
        }
        _buf.erase(0, cut);
        _buf_start = index;
    }

private:
    // Ensure the codepoint at absolute index `abs` is decoded into the window
    // (or that EOF was reached first).
    void ensure_loaded(size_t abs) {
        while (_buf_start + _buf.size() <= abs && decode_one()) {
        }
    }

    // Decode the next codepoint from the byte stream into the window. Returns
    // false at end of input.
    bool decode_one() {
        if (_stream_eof) {
            return false;
        }
        if (!ensure_bytes()) {
            _stream_eof = true;
            return false;
        }
        std::pair<char32_t, size_t> r =
            antlrcpp::Utf8::decode(std::string_view(_bytes).substr(_byte_pos));
        if (r.second == 0) {
            _stream_eof = true;  // defensive: never advance by zero
            return false;
        }
        // A 1-unit U+FFFD is the runtime's malformed-input marker (a valid
        // U+FFFD decodes from 3 units).
        if (!_lenient && r.first == 0xFFFD && r.second == 1) {
            throw std::runtime_error("invalid UTF-8 in " + _name);
        }
        _byte_pos += r.second;
        _buf.push_back(r.first);
        return true;
    }

    // Make at least 4 bytes (the maximum UTF-8 sequence length) available from
    // _byte_pos, or read until EOF. Looping matters for small blocks: a single
    // read may not cover a multi-byte sequence. Compacting before each read
    // makes a sequence straddling a block boundary contiguous.
    bool ensure_bytes() {
        while (_bytes.size() - _byte_pos < 4 && !_file_eof) {
            if (_byte_pos > 0) {
                _bytes.erase(0, _byte_pos);
                _byte_pos = 0;
            }
            size_t old = _bytes.size();
            _bytes.resize(old + _block);
            _in.read(&_bytes[old], static_cast<std::streamsize>(_block));
            std::streamsize got = _in.gcount();
            _bytes.resize(old + static_cast<size_t>(got));
            if (got < static_cast<std::streamsize>(_block)) {
                _file_eof = true;  // short read => end of file
            }
        }
        return _byte_pos < _bytes.size();
    }

    std::string _name;
    bool _lenient;
    size_t _block;

    // Decoded-codepoint window: _buf[k] is the codepoint at absolute index
    // _buf_start + k. _pos is the absolute index of LA(1).
    std::u32string _buf;
    size_t _buf_start = 0;
    size_t _pos = 0;
    size_t _mark_depth = 0;

    // Byte-decode state.
    std::ifstream _in;
    std::string _bytes;
    size_t _byte_pos = 0;
    bool _file_eof = false;    // no more bytes to read from the file
    bool _stream_eof = false;  // no more codepoints to decode
};

}  // namespace antlrope
