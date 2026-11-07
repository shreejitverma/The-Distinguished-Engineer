#!/usr/bin/env python3
"""Tree-sitter Concrete Syntax Tree (CST), S-expression query engine, and incremental re-parsing CLI.

Demonstrates:
1. Lexical tokenization and Concrete Syntax Tree (CST) construction with field bindings.
2. S-expression query language compilation with pattern matching, captures, and predicates.
3. Structural text-object extraction for modal editing (e.g. @function.outer, @function.inner).
4. Incremental tree editing (TSInputEdit semantics) and subtree reuse during incremental re-parsing.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple


@dataclasses.dataclass(frozen=True)
class Point:
    row: int
    column: int

    def __str__(self) -> str:
        return f"{self.row}:{self.column}"


@dataclasses.dataclass
class Node:
    id: int
    type: str
    is_named: bool
    start_byte: int
    end_byte: int
    start_point: Point
    end_point: Point
    children: List[Node] = dataclasses.field(default_factory=list)
    fields: Dict[str, Node] = dataclasses.field(default_factory=dict)
    parent: Optional[Node] = None

    def text(self, source: bytes) -> str:
        return source[self.start_byte : self.end_byte].decode("utf-8", errors="replace")

    def to_sexp(self, source: Optional[bytes] = None, depth: int = 0) -> str:
        if not self.is_named:
            if source is not None:
                val = self.text(source).replace('"', '\\"')
                return f'"{val}"'
            return f'"{self.type}"'
        
        prefix = f"({self.type}"
        parts = [prefix]
        
        for child in self.children:
            field_name = None
            for fname, fnode in self.fields.items():
                if fnode is child:
                    field_name = fname
                    break
            child_str = child.to_sexp(source, depth + 1)
            if field_name:
                parts.append(f"{field_name}: {child_str}")
            else:
                parts.append(child_str)
        
        return " ".join(parts) + ")"


@dataclasses.dataclass
class InputEdit:
    start_byte: int
    old_end_byte: int
    new_end_byte: int
    start_point: Point
    old_end_point: Point
    new_end_point: Point


# ---------------------------------------------------------------------------
# Lexer and CST Parser for a C-like Language Subset
# ---------------------------------------------------------------------------

class TokenType:
    IDENTIFIER = "identifier"
    PRIMITIVE_TYPE = "primitive_type"
    NUMBER = "number_literal"
    STRING = "string_literal"
    COMMENT = "comment"
    KEYWORD_RETURN = "return"
    LPAREN = "("
    RPAREN = ")"
    LBRACE = "{"
    RBRACE = "}"
    SEMI = ";"
    COMMA = ","
    EQUAL = "="
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    EQ_OP = "=="


KEYWORDS = {
    "int": TokenType.PRIMITIVE_TYPE,
    "void": TokenType.PRIMITIVE_TYPE,
    "char": TokenType.PRIMITIVE_TYPE,
    "return": TokenType.KEYWORD_RETURN,
}

PUNCTUATION = {
    "==": TokenType.EQ_OP,
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "{": TokenType.LBRACE,
    "}": TokenType.RBRACE,
    ";": TokenType.SEMI,
    ",": TokenType.COMMA,
    "=": TokenType.EQUAL,
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "*": TokenType.STAR,
    "/": TokenType.SLASH,
}


@dataclasses.dataclass
class Token:
    type: str
    is_named: bool
    text: str
    start_byte: int
    end_byte: int
    start_point: Point
    end_point: Point


def byte_to_point(src: bytes, offset: int) -> Point:
    lines = src[:offset].decode("utf-8").split("\n")
    return Point(len(lines) - 1, len(lines[-1]))


def tokenize(source_bytes: bytes, start_byte: int = 0, end_byte: Optional[int] = None) -> List[Token]:
    """Lex source_bytes[start_byte:end_byte]; offsets and points stay absolute.

    The lab assumes ASCII source, so a byte offset equals a character index.
    """
    tokens: List[Token] = []
    text = source_bytes.decode("utf-8")
    idx = start_byte
    start_pt = byte_to_point(source_bytes, start_byte)
    row = start_pt.row
    col = start_pt.column
    n = len(text) if end_byte is None else end_byte

    def advance(count: int = 1) -> None:
        nonlocal idx, row, col
        for _ in range(count):
            if idx < n:
                if text[idx] == "\n":
                    row += 1
                    col = 0
                else:
                    col += 1
                idx += 1

    while idx < n:
        ch = text[idx]
        if ch.isspace():
            advance(1)
            continue

        # Comments
        if ch == "/" and idx + 1 < n and text[idx + 1] == "/":
            start_b = idx
            start_p = Point(row, col)
            while idx < n and text[idx] != "\n":
                advance(1)
            tok_text = text[start_b:idx]
            tokens.append(Token(TokenType.COMMENT, True, tok_text, start_b, idx, start_p, Point(row, col)))
            continue

        # Two-character punctuation (==)
        if idx + 1 < n and text[idx : idx + 2] in PUNCTUATION:
            start_b = idx
            start_p = Point(row, col)
            op = text[idx : idx + 2]
            advance(2)
            tokens.append(Token(op, False, op, start_b, idx, start_p, Point(row, col)))
            continue

        # Single-character punctuation
        if ch in PUNCTUATION:
            start_b = idx
            start_p = Point(row, col)
            advance(1)
            tokens.append(Token(ch, False, ch, start_b, idx, start_p, Point(row, col)))
            continue

        # Numbers
        if ch.isdigit():
            start_b = idx
            start_p = Point(row, col)
            while idx < n and (text[idx].isdigit() or text[idx] in ".xXabcdefABCDEF"):
                advance(1)
            tok_text = text[start_b:idx]
            tokens.append(Token(TokenType.NUMBER, True, tok_text, start_b, idx, start_p, Point(row, col)))
            continue

        # Identifiers / Keywords
        if ch.isalpha() or ch == "_":
            start_b = idx
            start_p = Point(row, col)
            while idx < n and (text[idx].isalnum() or text[idx] == "_"):
                advance(1)
            tok_text = text[start_b:idx]
            if tok_text in KEYWORDS:
                ktype = KEYWORDS[tok_text]
                tokens.append(Token(ktype, True if ktype != TokenType.KEYWORD_RETURN else False, tok_text, start_b, idx, start_p, Point(row, col)))
            else:
                tokens.append(Token(TokenType.IDENTIFIER, True, tok_text, start_b, idx, start_p, Point(row, col)))
            continue

        # Fallback for single unknown char
        start_b = idx
        start_p = Point(row, col)
        advance(1)
        tokens.append(Token(ch, False, ch, start_b, idx, start_p, Point(row, col)))

    return tokens


class Parser:
    def __init__(self, tokens: List[Token], source_bytes: bytes, first_id: int = 1):
        self.tokens = tokens
        self.source_bytes = source_bytes
        self.pos = 0
        self.node_id_counter = first_id

    def _next_id(self) -> int:
        nid = self.node_id_counter
        self.node_id_counter += 1
        return nid

    def peek(self) -> Optional[Token]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def match(self, token_type: str) -> Optional[Token]:
        tok = self.peek()
        if tok and tok.type == token_type:
            self.pos += 1
            return tok
        return None

    def expect(self, token_type: str) -> Token:
        tok = self.match(token_type)
        if not tok:
            cur = self.peek()
            cur_desc = f"'{cur.type}' ({cur.text})" if cur else "EOF"
            raise SyntaxError(f"Expected '{token_type}' at token {self.pos}, found {cur_desc}")
        return tok

    def make_leaf(self, token: Token) -> Node:
        return Node(
            id=self._next_id(),
            type=token.type,
            is_named=token.is_named,
            start_byte=token.start_byte,
            end_byte=token.end_byte,
            start_point=token.start_point,
            end_point=token.end_point,
        )

    def parse_translation_unit(self) -> Node:
        return self.make_root(self.parse_top_level_items())

    def make_root(self, children: List[Node]) -> Node:
        start_b = children[0].start_byte if children else 0
        start_p = children[0].start_point if children else Point(0, 0)
        end_b = children[-1].end_byte if children else 0
        end_p = children[-1].end_point if children else Point(0, 0)
        root = Node(
            id=self._next_id(),
            type="translation_unit",
            is_named=True,
            start_byte=start_b,
            end_byte=end_b,
            start_point=start_p,
            end_point=end_p,
            children=children,
        )
        for c in children:
            c.parent = root
        return root

    def parse_top_level_items(self) -> List[Node]:
        children: List[Node] = []
        while self.peek():
            tok = self.peek()
            assert tok is not None
            if tok.type == TokenType.COMMENT:
                self.pos += 1
                children.append(self.make_leaf(tok))
            elif tok.type == TokenType.PRIMITIVE_TYPE:
                # Could be a function definition or global declaration
                # Lookahead to check if this is function definition: type identifier '('
                if self.pos + 2 < len(self.tokens) and self.tokens[self.pos + 2].type == TokenType.LPAREN:
                    fn_node = self.parse_function_definition()
                    children.append(fn_node)
                else:
                    decl_node = self.parse_declaration()
                    children.append(decl_node)
            else:
                # Unexpected token: consume as error/unknown leaf
                self.pos += 1
                children.append(self.make_leaf(tok))
        return children

    def parse_function_definition(self) -> Node:
        type_tok = self.expect(TokenType.PRIMITIVE_TYPE)
        type_node = self.make_leaf(type_tok)

        name_tok = self.expect(TokenType.IDENTIFIER)
        name_node = self.make_leaf(name_tok)

        param_list_node = self.parse_parameter_list()
        body_node = self.parse_compound_statement()

        children = [type_node, name_node, param_list_node, body_node]
        fields = {
            "type": type_node,
            "name": name_node,
            "parameters": param_list_node,
            "body": body_node,
        }

        fn = Node(
            id=self._next_id(),
            type="function_definition",
            is_named=True,
            start_byte=type_node.start_byte,
            end_byte=body_node.end_byte,
            start_point=type_node.start_point,
            end_point=body_node.end_point,
            children=children,
            fields=fields,
        )
        for c in children:
            c.parent = fn
        return fn

    def parse_parameter_list(self) -> Node:
        lparen = self.expect(TokenType.LPAREN)
        lparen_node = self.make_leaf(lparen)
        children: List[Node] = [lparen_node]

        while self.peek() and self.peek().type != TokenType.RPAREN: # type: ignore
            tok = self.peek()
            assert tok is not None
            if tok.type == TokenType.PRIMITIVE_TYPE:
                param_type = self.make_leaf(self.expect(TokenType.PRIMITIVE_TYPE))
                param_name = self.make_leaf(self.expect(TokenType.IDENTIFIER))
                pdecl_children = [param_type, param_name]
                pdecl = Node(
                    id=self._next_id(),
                    type="parameter_declaration",
                    is_named=True,
                    start_byte=param_type.start_byte,
                    end_byte=param_name.end_byte,
                    start_point=param_type.start_point,
                    end_point=param_name.end_point,
                    children=pdecl_children,
                    fields={"type": param_type, "name": param_name},
                )
                for c in pdecl_children:
                    c.parent = pdecl
                children.append(pdecl)

                if self.peek() and self.peek().type == TokenType.COMMA: # type: ignore
                    comma = self.make_leaf(self.expect(TokenType.COMMA))
                    children.append(comma)
            else:
                break

        rparen = self.expect(TokenType.RPAREN)
        rparen_node = self.make_leaf(rparen)
        children.append(rparen_node)

        plist = Node(
            id=self._next_id(),
            type="parameter_list",
            is_named=True,
            start_byte=lparen_node.start_byte,
            end_byte=rparen_node.end_byte,
            start_point=lparen_node.start_point,
            end_point=rparen_node.end_point,
            children=children,
        )
        for c in children:
            c.parent = plist
        return plist

    def parse_compound_statement(self) -> Node:
        lbrace = self.expect(TokenType.LBRACE)
        lbrace_node = self.make_leaf(lbrace)
        children: List[Node] = [lbrace_node]

        while self.peek() and self.peek().type != TokenType.RBRACE: # type: ignore
            tok = self.peek()
            assert tok is not None
            if tok.type == TokenType.COMMENT:
                children.append(self.make_leaf(self.expect(TokenType.COMMENT)))
            elif tok.type == TokenType.PRIMITIVE_TYPE:
                children.append(self.parse_declaration())
            elif tok.type == TokenType.KEYWORD_RETURN:
                children.append(self.parse_return_statement())
            elif tok.type == TokenType.IDENTIFIER:
                # Expression statement or assignment
                children.append(self.parse_expression_statement())
            else:
                # Unknown statement token
                children.append(self.make_leaf(tok))
                self.pos += 1

        rbrace = self.expect(TokenType.RBRACE)
        rbrace_node = self.make_leaf(rbrace)
        children.append(rbrace_node)

        comp = Node(
            id=self._next_id(),
            type="compound_statement",
            is_named=True,
            start_byte=lbrace_node.start_byte,
            end_byte=rbrace_node.end_byte,
            start_point=lbrace_node.start_point,
            end_point=rbrace_node.end_point,
            children=children,
        )
        for c in children:
            c.parent = comp
        return comp

    def parse_declaration(self) -> Node:
        type_tok = self.expect(TokenType.PRIMITIVE_TYPE)
        type_node = self.make_leaf(type_tok)

        decl_tok = self.expect(TokenType.IDENTIFIER)
        decl_node = self.make_leaf(decl_tok)

        children: List[Node] = [type_node, decl_node]
        fields: Dict[str, Node] = {"type": type_node, "declarator": decl_node}

        if self.peek() and self.peek().type == TokenType.EQUAL: # type: ignore
            eq_tok = self.expect(TokenType.EQUAL)
            eq_node = self.make_leaf(eq_tok)
            init_node = self.parse_expression()
            children.extend([eq_node, init_node])
            fields["value"] = init_node

        semi_tok = self.expect(TokenType.SEMI)
        semi_node = self.make_leaf(semi_tok)
        children.append(semi_node)

        decl = Node(
            id=self._next_id(),
            type="declaration",
            is_named=True,
            start_byte=type_node.start_byte,
            end_byte=semi_node.end_byte,
            start_point=type_node.start_point,
            end_point=semi_node.end_point,
            children=children,
            fields=fields,
        )
        for c in children:
            c.parent = decl
        return decl

    def parse_return_statement(self) -> Node:
        ret_tok = self.expect(TokenType.KEYWORD_RETURN)
        ret_node = self.make_leaf(ret_tok)
        children: List[Node] = [ret_node]

        expr_node = None
        if self.peek() and self.peek().type != TokenType.SEMI: # type: ignore
            expr_node = self.parse_expression()
            children.append(expr_node)

        semi_tok = self.expect(TokenType.SEMI)
        semi_node = self.make_leaf(semi_tok)
        children.append(semi_node)

        fields: Dict[str, Node] = {}
        if expr_node:
            fields["argument"] = expr_node

        ret = Node(
            id=self._next_id(),
            type="return_statement",
            is_named=True,
            start_byte=ret_node.start_byte,
            end_byte=semi_node.end_byte,
            start_point=ret_node.start_point,
            end_point=semi_node.end_point,
            children=children,
            fields=fields,
        )
        for c in children:
            c.parent = ret
        return ret

    def parse_expression_statement(self) -> Node:
        expr = self.parse_expression()
        semi_tok = self.expect(TokenType.SEMI)
        semi_node = self.make_leaf(semi_tok)
        children = [expr, semi_node]
        es = Node(
            id=self._next_id(),
            type="expression_statement",
            is_named=True,
            start_byte=expr.start_byte,
            end_byte=semi_node.end_byte,
            start_point=expr.start_point,
            end_point=semi_node.end_point,
            children=children,
        )
        for c in children:
            c.parent = es
        return es

    def parse_expression(self) -> Node:
        left = self.parse_primary_expression()

        # Binary operator check
        tok = self.peek()
        if tok and tok.type in (TokenType.PLUS, TokenType.MINUS, TokenType.STAR, TokenType.SLASH, TokenType.EQ_OP, TokenType.EQUAL):
            op_tok = self.tokens[self.pos]
            self.pos += 1
            op_node = self.make_leaf(op_tok)
            right = self.parse_expression()
            children = [left, op_node, right]
            fields = {"left": left, "operator": op_node, "right": right}
            bin_node = Node(
                id=self._next_id(),
                type="assignment_expression" if op_tok.type == TokenType.EQUAL else "binary_expression",
                is_named=True,
                start_byte=left.start_byte,
                end_byte=right.end_byte,
                start_point=left.start_point,
                end_point=right.end_point,
                children=children,
                fields=fields,
            )
            for c in children:
                c.parent = bin_node
            return bin_node

        return left

    def parse_primary_expression(self) -> Node:
        tok = self.peek()
        if not tok:
            raise SyntaxError("Unexpected end of file while parsing primary expression")
        if tok.type in (TokenType.IDENTIFIER, TokenType.NUMBER):
            self.pos += 1
            return self.make_leaf(tok)
        if tok.type == TokenType.LPAREN:
            lparen = self.make_leaf(self.expect(TokenType.LPAREN))
            inner = self.parse_expression()
            rparen = self.make_leaf(self.expect(TokenType.RPAREN))
            children = [lparen, inner, rparen]
            paren_expr = Node(
                id=self._next_id(),
                type="parenthesized_expression",
                is_named=True,
                start_byte=lparen.start_byte,
                end_byte=rparen.end_byte,
                start_point=lparen.start_point,
                end_point=rparen.end_point,
                children=children,
            )
            for c in children:
                c.parent = paren_expr
            return paren_expr
        self.pos += 1
        return self.make_leaf(tok)


# ---------------------------------------------------------------------------
# S-Expression Query Compiler and Pattern Matcher
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Predicate:
    operator: str # eq?, not-eq?, match?, not-match?, any-of?
    capture_name: str
    args: List[str]

    def evaluate(self, captures: Dict[str, List[Node]], source: bytes) -> bool:
        nodes = captures.get(self.capture_name, [])
        if not nodes:
            return False
        node_text = nodes[0].text(source)

        if self.operator == "eq?":
            return node_text == self.args[0]
        elif self.operator == "not-eq?":
            return node_text != self.args[0]
        elif self.operator == "match?":
            pattern = self.args[0]
            return re.search(pattern, node_text) is not None
        elif self.operator == "not-match?":
            pattern = self.args[0]
            return re.search(pattern, node_text) is None
        elif self.operator == "any-of?":
            return node_text in self.args
        return True


@dataclasses.dataclass
class QueryPattern:
    node_type: str
    field_name: Optional[str] = None
    capture_name: Optional[str] = None
    child_patterns: List[QueryPattern] = dataclasses.field(default_factory=list)
    predicates: List[Predicate] = dataclasses.field(default_factory=list)
    quantifier: Optional[str] = None # None, '?', '*', '+'


class QueryError(Exception):
    """Raised for query syntax real Tree-sitter would also reject."""


class QueryParser:
    def __init__(self, query_str: str):
        self.text = query_str
        self.tokens = self._tokenize(query_str)
        self.pos = 0

    def _tokenize(self, s: str) -> List[str]:
        # Tokenize S-expression query string
        pattern = r'(\(|\)|"[^"]*"|@[a-zA-Z0-9_\.]+|#[a-zA-Z0-9_\.\?-]+|[a-zA-Z0-9_\-]+:|[a-zA-Z0-9_\.\?\*\+]+)'
        tokens: List[str] = []
        for match in re.finditer(pattern, s):
            tok = match.group(0).strip()
            if tok:
                tokens.append(tok)
        return tokens

    def peek(self) -> Optional[str]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def match(self) -> str:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse_queries(self) -> List[QueryPattern]:
        patterns: List[QueryPattern] = []
        while self.peek():
            if self.peek() == "(":
                p = self.parse_pattern()
                patterns.append(p)
            else:
                self.pos += 1
        return patterns

    def parse_pattern(self, field_name: Optional[str] = None) -> QueryPattern:
        assert self.match() == "("
        if self.peek() == "(":
            return self.parse_group(field_name)
        first = self.match()

        predicates: List[Predicate] = []
        child_patterns: List[QueryPattern] = []
        capture_name: Optional[str] = None
        quantifier: Optional[str] = None

        node_type = first

        while self.peek() and self.peek() != ")":
            nxt = self.peek()
            assert nxt is not None
            if nxt.endswith(":"):
                fname = self.match()[:-1]
                child = self.parse_pattern(field_name=fname)
                child_patterns.append(child)
            elif nxt == "(":
                # Child pattern or predicate
                if self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].startswith("#"):
                    # Predicate
                    pred = self.parse_predicate()
                    predicates.append(pred)
                else:
                    child = self.parse_pattern()
                    child_patterns.append(child)
            elif nxt.startswith("@"):
                # Capture on this node or previous child
                cap = self.match()[1:]
                if not child_patterns:
                    # Real Tree-sitter writes (identifier) @x; a capture inside the
                    # parentheses of a childless node pattern is a syntax error.
                    raise QueryError(f"Invalid syntax: capture @{cap} inside '({node_type}' must follow the closing parenthesis")
                child_patterns[-1].capture_name = cap
            elif nxt in ("?", "*", "+"):
                q = self.match()
                if child_patterns:
                    child_patterns[-1].quantifier = q
            else:
                self.pos += 1

        assert self.match() == ")"

        # Check for trailing capture or quantifier on this pattern (e.g. (pattern) @outer)
        while self.peek() and (self.peek().startswith("@") or self.peek() in ("?", "*", "+")): # type: ignore
            tok = self.match()
            if tok.startswith("@"):
                capture_name = tok[1:]
            elif tok in ("?", "*", "+"):
                quantifier = tok

        return QueryPattern(
            node_type=node_type,
            field_name=field_name,
            capture_name=capture_name,
            child_patterns=child_patterns,
            predicates=predicates,
            quantifier=quantifier,
        )

    def parse_group(self, field_name: Optional[str]) -> QueryPattern:
        """Parse ((node) @cap (#pred? @cap ...)); the opening '(' is already consumed."""
        inner = self.parse_pattern(field_name)
        while self.peek() == "(":
            inner.predicates.append(self.parse_predicate())
        if self.peek() != ")":
            raise QueryError("Invalid syntax: expected ')' to close the pattern group")
        self.match()
        return inner

    def parse_predicate(self) -> Predicate:
        assert self.match() == "("
        pred_op = self.match()
        if pred_op.startswith("#"):
            pred_op = pred_op[1:]
        cap_tok = self.match()
        capture_name = cap_tok[1:] if cap_tok.startswith("@") else cap_tok
        args: List[str] = []
        while self.peek() and self.peek() != ")":
            tok = self.match()
            if tok.startswith('"') and tok.endswith('"'):
                tok = tok[1:-1]
            args.append(tok)
        assert self.match() == ")"
        return Predicate(pred_op, capture_name, args)


@dataclasses.dataclass
class MatchResult:
    pattern_index: int
    captures: List[Tuple[str, Node]]


class QueryCursor:
    def __init__(self, patterns: List[QueryPattern], source_bytes: bytes):
        self.patterns = patterns
        self.source_bytes = source_bytes

    def matches(self, root: Node) -> Iterator[MatchResult]:
        for idx, pattern in enumerate(self.patterns):
            for m in self._search_node(root, pattern, idx):
                yield m

    def _search_node(self, node: Node, pattern: QueryPattern, pattern_index: int) -> Iterator[MatchResult]:
        captures_dict: Dict[str, List[Node]] = {}
        if self._match_pattern(node, pattern, captures_dict):
            # Check predicates
            pred_ok = True
            for p in pattern.predicates:
                if not p.evaluate(captures_dict, self.source_bytes):
                    pred_ok = False
                    break
            if pred_ok:
                flat_caps: List[Tuple[str, Node]] = []
                for cname, nlist in captures_dict.items():
                    for n in nlist:
                        flat_caps.append((cname, n))
                # Sort captures by start_byte
                flat_caps.sort(key=lambda item: item[1].start_byte)
                yield MatchResult(pattern_index=pattern_index, captures=flat_caps)

        # Recursively search children
        for child in node.children:
            yield from self._search_node(child, pattern, pattern_index)

    def _match_pattern(self, node: Node, pattern: QueryPattern, captures: Dict[str, List[Node]]) -> bool:
        if pattern.node_type != "_" and node.type != pattern.node_type:
            return False

        if pattern.capture_name:
            captures.setdefault(pattern.capture_name, []).append(node)

        # Match child patterns
        if pattern.child_patterns:
            named_children = [c for c in node.children if c.is_named]
            child_idx = 0
            for cp in pattern.child_patterns:
                matched = False
                if cp.field_name:
                    # Look for field in node.fields
                    fnode = node.fields.get(cp.field_name)
                    if fnode and self._match_pattern(fnode, cp, captures):
                        matched = True
                else:
                    # Sequential child match
                    while child_idx < len(named_children):
                        cand = named_children[child_idx]
                        child_idx += 1
                        if self._match_pattern(cand, cp, captures):
                            matched = True
                            break
                if not matched and cp.quantifier not in ("?", "*"):
                    return False

        return True


# ---------------------------------------------------------------------------
# Incremental Tree Editing and Subtree Reuse
# ---------------------------------------------------------------------------

def apply_tree_edit(root: Node, edit: InputEdit) -> None:
    """Adjusts byte and point coordinates of existing nodes following ts_tree_edit semantics."""
    delta_bytes = edit.new_end_byte - edit.old_end_byte
    delta_rows = edit.new_end_point.row - edit.old_end_point.row
    delta_cols = edit.new_end_point.column - edit.old_end_point.column

    def visit(n: Node) -> None:
        if n.start_byte >= edit.old_end_byte:
            # Entirely after edit: shift both start and end
            n.start_byte += delta_bytes
            n.end_byte += delta_bytes
            new_row = n.start_point.row + delta_rows
            new_col = (n.start_point.column + delta_cols) if n.start_point.row == edit.old_end_point.row else n.start_point.column
            n.start_point = Point(new_row, new_col)

            new_end_row = n.end_point.row + delta_rows
            new_end_col = (n.end_point.column + delta_cols) if n.end_point.row == edit.old_end_point.row else n.end_point.column
            n.end_point = Point(new_end_row, new_end_col)
        elif n.end_byte > edit.start_byte:
            # Overlaps with edit
            n.end_byte += delta_bytes
            new_end_row = n.end_point.row + delta_rows
            new_end_col = (n.end_point.column + delta_cols) if n.end_point.row == edit.old_end_point.row else n.end_point.column
            n.end_point = Point(new_end_row, new_end_col)

        for child in n.children:
            visit(child)

    visit(root)


def shape(node: Node) -> Tuple[Any, ...]:
    """Structural fingerprint of a subtree: type, coordinates, and child shapes with field names."""
    field_of = {id(v): k for k, v in node.fields.items()}
    return (
        node.type,
        node.start_byte,
        node.end_byte,
        node.start_point.row,
        node.start_point.column,
        node.end_point.row,
        node.end_point.column,
        tuple((field_of.get(id(c)), shape(c)) for c in node.children),
    )


def incremental_reparse(old_root: Node, new_source_bytes: bytes, edit: InputEdit) -> Tuple[Node, Dict[str, Any]]:
    """Re-parse after apply_tree_edit by carrying over untouched top-level subtrees.

    Top-level children that end before the edit, or start after its new end, are reused as the
    same Node objects. Only the bytes between them are lexed and parsed again.
    Real Tree-sitter does this at every depth and also tracks lookahead; this lab does it for
    top-level items only and keeps a one-byte safety gap on each side of the edit.
    """
    old_children = old_root.children
    prefix = 0
    while prefix < len(old_children) and old_children[prefix].end_byte < edit.start_byte:
        prefix += 1
    suffix = 0
    while (
        suffix < len(old_children) - prefix
        and old_children[len(old_children) - 1 - suffix].start_byte > edit.new_end_byte
    ):
        suffix += 1

    dirty_start = old_children[prefix - 1].end_byte if prefix else 0
    dirty_end = old_children[len(old_children) - suffix].start_byte if suffix else len(new_source_bytes)

    dirty_tokens = tokenize(new_source_bytes, dirty_start, dirty_end)
    max_id = max(n.id for n in old_children) if old_children else 0
    parser = Parser(dirty_tokens, new_source_bytes, first_id=max_id + 1)
    middle = parser.parse_top_level_items()

    children = old_children[:prefix] + middle + old_children[len(old_children) - suffix :]
    new_root = parser.make_root(children)

    # Reuse is judged by object identity, not by byte ranges.
    old_ids = {id(c) for c in old_children}
    reused = [c for c in new_root.children if id(c) in old_ids]
    reparsed = [c for c in new_root.children if id(c) not in old_ids]

    def fn_names(nodes: List[Node]) -> List[str]:
        return [n.fields["name"].text(new_source_bytes) for n in nodes if n.type == "function_definition"]

    reused_fns = fn_names(reused)
    reparsed_fns = fn_names(reparsed)
    total_fns = len(reused_fns) + len(reparsed_fns)

    # Ground truth: an incremental tree must equal a from-scratch parse of the same bytes.
    full_tokens = tokenize(new_source_bytes)
    full_root = Parser(full_tokens, new_source_bytes).parse_translation_unit()
    matches_full = shape(new_root) == shape(full_root)

    stats = {
        "total_functions": total_fns,
        "reused_subtrees": reused_fns,
        "reparsed_subtrees": reparsed_fns,
        "reuse_ratio": len(reused_fns) / total_fns if total_fns else 0.0,
        "tokens_lexed": len(dirty_tokens),
        "tokens_total": len(full_tokens),
        "dirty_range": (dirty_start, dirty_end),
        "matches_full_parse": matches_full,
        "reused_ids": {id(n) for n in reused},
    }
    return new_root, stats


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

def run_dump_tree(source_path: str) -> None:
    with open(source_path, "rb") as f:
        src = f.read()
    tokens = tokenize(src)
    parser = Parser(tokens, src)
    root = parser.parse_translation_unit()
    print("=== Concrete Syntax Tree (S-expression) ===")
    print(root.to_sexp(src))
    print()
    print("=== Syntax Tree Node Summary ===")
    print(f"Root: {root.type} [{root.start_point} - {root.end_point}] ({root.end_byte - root.start_byte} bytes)")
    for i, child in enumerate(root.children):
        cname = child.fields.get("name")
        cname_str = f" name='{cname.text(src)}'" if cname else ""
        print(f"  Child {i}: {child.type}{cname_str} [{child.start_point} - {child.end_point}]")


def run_query(source_path: str, query_text: str) -> None:
    with open(source_path, "rb") as f:
        src = f.read()
    tokens = tokenize(src)
    parser = Parser(tokens, src)
    root = parser.parse_translation_unit()

    qp = QueryParser(query_text)
    patterns = qp.parse_queries()
    cursor = QueryCursor(patterns, src)

    print(f"=== Query Execution: {len(patterns)} pattern(s) ===")
    match_count = 0
    for match in cursor.matches(root):
        match_count += 1
        print(f"Match #{match_count} (pattern {match.pattern_index}):")
        for cname, node in match.captures:
            text_preview = node.text(src).replace("\n", " ").strip()
            if len(text_preview) > 30:
                text_preview = text_preview[:27] + "..."
            print(f"  @{cname:<18} [{node.start_point} - {node.end_point}] ({node.type}): \"{text_preview}\"")
    print(f"Total matches: {match_count}")


def run_incremental_demo(source_path: str) -> None:
    with open(source_path, "rb") as f:
        initial_src = f.read()

    tokens = tokenize(initial_src)
    parser = Parser(tokens, initial_src)
    root_v1 = parser.parse_translation_unit()

    print("=== Initial Parse (Tree v1) ===")
    print(f"Source size: {len(initial_src)} bytes")
    for child in root_v1.children:
        if child.type == "function_definition":
            fname = child.fields["name"].text(initial_src)
            print(f"  function: {fname:<22} bytes: [{child.start_byte:3d}..{child.end_byte:3d}] span: [{child.start_point} - {child.end_point}]")

    # Perform edit: replace the body of process_transaction
    target_fn = "process_transaction"
    fn_node = [c for c in root_v1.children if c.type == "function_definition" and c.fields["name"].text(initial_src) == target_fn][0]
    body_node = fn_node.fields["body"]

    # Target edit inside body: replace 'int status = 0;' with 'int status = 100; int retries = 3;'
    old_target = b"int status = 0;"
    new_target = b"int status = 100; int retries = 3;"

    edit_offset = initial_src.index(old_target)
    edit_old_end = edit_offset + len(old_target)
    edit_new_end = edit_offset + len(new_target)

    # Compute points
    start_p = byte_to_point(initial_src, edit_offset)
    old_end_p = byte_to_point(initial_src, edit_old_end)

    edited_src = initial_src[:edit_offset] + new_target + initial_src[edit_old_end:]
    new_end_p = byte_to_point(edited_src, edit_new_end)

    edit = InputEdit(
        start_byte=edit_offset,
        old_end_byte=edit_old_end,
        new_end_byte=edit_new_end,
        start_point=start_p,
        old_end_point=old_end_p,
        new_end_point=new_end_p,
    )

    print()
    print("=== Applied TSInputEdit ===")
    print(f"  start_byte:    {edit.start_byte} (start_point: {edit.start_point})")
    print(f"  old_end_byte:  {edit.old_end_byte} (old_end_point: {edit.old_end_point})")
    print(f"  new_end_byte:  {edit.new_end_byte} (new_end_point: {edit.new_end_point})")
    print(f"  byte delta:    {edit.new_end_byte - edit.old_end_byte:+d} bytes")

    # Shift tree
    apply_tree_edit(root_v1, edit)

    # Re-parse incrementally
    root_v2, stats = incremental_reparse(root_v1, edited_src, edit)

    print()
    print("=== Incremental Re-parse (Tree v2) ===")
    print(f"New source size: {len(edited_src)} bytes")
    lo, hi = stats["dirty_range"]
    print(f"Dirty range re-lexed: bytes [{lo}..{hi}) -> {stats['tokens_lexed']} of {stats['tokens_total']} tokens")
    for child in root_v2.children:
        if child.type == "function_definition":
            fname = child.fields["name"].text(edited_src)
            status = "REUSED (same object as v1)" if id(child) in stats["reused_ids"] else "REPARSED (new object)"
            print(f"  function: {fname:<22} bytes: [{child.start_byte:3d}..{child.end_byte:3d}] span: [{child.start_point} - {child.end_point}] -> {status}")

    print()
    print("=== Subtree Reuse Statistics ===")
    print(f"Total top-level functions: {stats['total_functions']}")
    print(f"Reused subtrees:           {len(stats['reused_subtrees'])} {stats['reused_subtrees']}")
    print(f"Re-parsed subtrees:        {len(stats['reparsed_subtrees'])} {stats['reparsed_subtrees']}")
    print(f"Subtree reuse ratio:       {stats['reuse_ratio'] * 100:.1f}%")
    print(f"Tree v2 equals a from-scratch parse: {'yes' if stats['matches_full_parse'] else 'NO'}")
    if not stats["matches_full_parse"]:
        raise SystemExit("incremental tree differs from a full re-parse")


def main() -> None:
    ap = argparse.ArgumentParser(description="Tree-sitter Query and Incremental Parser CLI")
    sub = ap.add_subparsers(dest="command", required=True)

    p_dump = sub.add_parser("dump-tree", help="Dump Concrete Syntax Tree")
    p_dump.add_argument("file", help="Source file")

    p_query = sub.add_parser("query", help="Execute S-expression query")
    p_query.add_argument("file", help="Source file")
    p_query.add_argument("--query", "-q", required=True, help="S-expression query string")

    p_demo = sub.add_parser("demo-incremental", help="Run incremental re-parsing demo")
    p_demo.add_argument("file", help="Source file")

    args = ap.parse_args()

    if args.command == "dump-tree":
        run_dump_tree(args.file)
    elif args.command == "query":
        try:
            run_query(args.file, args.query)
        except QueryError as exc:
            print(f"QueryError: {exc}", file=sys.stderr)
            raise SystemExit(2)
    elif args.command == "demo-incremental":
        run_incremental_demo(args.file)


if __name__ == "__main__":
    main()
