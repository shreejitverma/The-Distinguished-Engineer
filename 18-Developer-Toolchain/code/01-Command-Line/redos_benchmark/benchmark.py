#!/usr/bin/env python3
"""benchmark.py - Evaluates polynomial ReDoS, Cloudflare wildcard cascade,

Python re atomic/possessive mitigations, and literal prefiltering.

The polynomial and wildcard tables run two tiny engines over a Thompson NFA
compiled from a token list: a recursive backtracker and a Thompson simulation,
both counting their own steps.
Every table is asserted against its closed form, so a broken engine or a
broken model aborts the lab with a traceback and a nonzero exit status.
"""

from __future__ import annotations
import re
import sys
import time

ANY = None  # the '.' wildcard in a token
State = list  # [kind, char, out, out1]; kind is "char", "split", or "match"


def compile_tokens(tokens: list[tuple[str | None, bool]]) -> tuple[list[State], int]:
  """Compiles (char, starred) tokens into a Thompson NFA; returns (states, start)."""
  states: list[State] = [["match", None, None, None]]
  nxt = 0
  for ch, star in reversed(tokens):
    if star:
      c = len(states)
      states.append(["char", ch, None, None])
      s = len(states)
      states.append(["split", None, c, nxt])
      states[c][2] = s
      nxt = s
    else:
      c = len(states)
      states.append(["char", ch, nxt, None])
      nxt = c
  return states, nxt


def char_matches(state: State, ch: str) -> bool:
  return state[1] is ANY or state[1] == ch


def backtrack(
    states: list[State], start: int, text: str, watch: int = -1
) -> tuple[bool, int, int]:
  """Recursive depth-first matcher; returns (matched, steps, visits to `watch`)."""
  steps = 0
  visits = 0

  def go(s: int, pos: int) -> bool:
    nonlocal steps, visits
    steps += 1
    if s == watch:
      visits += 1
    kind, _, out, out1 = states[s]
    if kind == "match":
      return True
    if kind == "split":
      return go(out, pos) or go(out1, pos)
    if pos < len(text) and char_matches(states[s], text[pos]):
      return go(out, pos + 1)
    return False

  matched = go(start, 0)
  return matched, steps, visits


def thompson(states: list[State], start: int, text: str) -> tuple[bool, int]:
  """Thompson simulation with a per-state generation mark; returns (matched, steps)."""
  steps = 0
  mark: dict[int, int] = {}
  gen = 1

  def add(lst: list[int], s: int) -> None:
    nonlocal steps
    if mark.get(s) == gen:
      return
    mark[s] = gen
    steps += 1
    if states[s][0] == "split":
      add(lst, states[s][2])
      add(lst, states[s][3])
      return
    lst.append(s)

  clist: list[int] = []
  add(clist, start)
  for ch in text:
    gen += 1
    nlist: list[int] = []
    for s in clist:
      if states[s][0] == "char" and char_matches(states[s], ch):
        add(nlist, states[s][2])
    clist = nlist
    if not clist:
      break
  return any(states[s][0] == "match" for s in clist), steps


def evaluate_polynomial_backtracking() -> None:
  print("=== Polynomial Backtracking Benchmark (Quadratic O(N^2)) ===")
  print(
      "Grammar: a*a*x (overlapping repetitions) against non-matching payload"
      " a^N + '!'"
  )
  print()
  print(f"{'N':<4} | {'Thompson Steps':<14} | {'Backtrack Steps':<15} | Ratio")
  print("-----+----------------+-----------------+-------")

  states, start = compile_tokens([("a", True), ("a", True), ("x", False)])
  prev_bt = None
  for n in (10, 20, 40, 80):
    text = "a" * n + "!"
    t_matched, th_steps = thompson(states, start, text)
    b_matched, bt_steps, _ = backtrack(states, start, text)
    assert not t_matched and not b_matched, "both engines must reject a^N!"
    assert th_steps == 5 * (n + 1), (n, th_steps)
    assert bt_steps == (3 * n + 10) * (n + 1) // 2, (n, bt_steps)
    ratio_str = "-" if prev_bt is None else f"{bt_steps / prev_bt:.2f}x"
    prev_bt = bt_steps
    print(f"{n:<4} | {th_steps:<14} | {bt_steps:<15} | {ratio_str}")
  print(
      "Observation: Doubling N quadruples backtracking steps (approaching"
      " 4.00x), proving O(N^2)."
  )
  print()


def evaluate_cloudflare_wildcards() -> None:
  print("=== Cloudflare Outage (July 2, 2019) Wildcard Cascade ===")
  print(
      "Grammar: .*.*= (adjacent unanchored wildcards) against payload a^N (no"
      " '=')"
  )
  print(
      "Partitions = times the engine reaches '=' (measured), one per way to"
      " split a^N across the wildcards"
  )
  print()
  print(
      f"{'N':<4} | {'Thompson Steps':<14} | {'Backtrack Steps':<15} |"
      f" {'Partitions .*.*=':<16} | {'Partitions .*.*.*=':<18}"
  )
  print("-----+----------------+-----------------+------------------+-------------------")

  two, two_start = compile_tokens([(ANY, True), (ANY, True), ("=", False)])
  three, three_start = compile_tokens(
      [(ANY, True), (ANY, True), (ANY, True), ("=", False)]
  )
  eq_two = next(i for i, s in enumerate(two) if s[0] == "char" and s[1] == "=")
  eq_three = next(
      i for i, s in enumerate(three) if s[0] == "char" and s[1] == "="
  )
  for n in (10, 20, 30, 40):
    text = "a" * n
    t_matched, th_steps = thompson(two, two_start, text)
    b_matched, bt_steps, parts2 = backtrack(two, two_start, text, eq_two)
    _, _, parts3 = backtrack(three, three_start, text, eq_three)
    assert not t_matched and not b_matched, "no '=' in the payload"
    assert th_steps == 5 * (n + 1), (n, th_steps)
    assert parts2 == (n + 1) * (n + 2) // 2, (n, parts2)
    assert parts3 == (n + 1) * (n + 2) * (n + 3) // 6, (n, parts3)
    print(
        f"{n:<4} | {th_steps:<14} | {bt_steps:<15} | {parts2:<16} |"
        f" {parts3:<18}"
    )
  print(
      "Root Cause: Under PCRE backtracking, each added adjacent '.*' raises the"
      " partition count by one polynomial degree (O(N^2), O(N^3), ...)."
  )
  print()


def evaluate_python_re_defenses() -> None:
  print("=== Python 3 re Engine Modern Mitigation Suite ===")

  # Semantic controls: atomic and possessive forms refuse to give characters
  # back, so a pattern that needs the give-back fails; the plain form matches.
  assert re.match(r"(a+)a", "aaa") is not None, "plain a+ must give back"
  assert re.match(r"(?>a+)a", "aaa") is None, "atomic group must not give back"
  assert re.match(r"a++a", "aaa") is None, "possessive must not give back"
  print("0. Controls on 'aaa': (a+)a matches; (?>a+)a and a++a do not")

  payload = "a" * 1000 + "!"

  # 1. Atomic grouping: (?>...)
  # Prevents backtracking into the matched group once matched
  t0 = time.perf_counter()
  m_atomic = re.match(r"^(?>a+)+$", payload)
  atomic_s = time.perf_counter() - t0
  assert m_atomic is None and atomic_s < 5.0, (m_atomic, atomic_s)
  print(
      "1. Atomic grouping ^(?>a+)+$ on 1000 chars: match ="
      f" {m_atomic} (ReDoS prevented)"
  )

  # 2. Possessive quantifier: ++
  # Consumes greedily and discards backtrack points
  t0 = time.perf_counter()
  m_possessive = re.match(r"^(a+)++$", payload)
  possessive_s = time.perf_counter() - t0
  assert m_possessive is None and possessive_s < 5.0, (
      m_possessive,
      possessive_s,
  )
  print(
      "2. Possessive quantifier ^(a+)++$ on 1000 chars: match ="
      f" {m_possessive} (ReDoS prevented)"
  )
  print()


def extract_prefix_literal(pattern: str) -> str:
  """Extracts exact literal prefix before metacharacters."""
  lit: list[str] = []
  meta = set(".*+?|^$()[]{}")
  i = 0
  while i < len(pattern):
    if pattern[i] == "\\":
      if i + 1 < len(pattern):
        lit.append(pattern[i + 1])
        i += 2
        continue
      break
    elif pattern[i] in meta:
      break
    else:
      lit.append(pattern[i])
      i += 1
  return "".join(lit)


def evaluate_literal_prefiltering() -> None:
  print("=== Substring Literal Prefilter Acceleration ===")
  pattern = r"POST /api/v1/checkout.*"
  prefix = extract_prefix_literal(pattern)
  assert prefix == "POST /api/v1/checkout", prefix
  print(f"Pattern: {pattern}")
  print(f"Extracted mandatory literal prefix: '{prefix}'")

  haystack_reject = "GET /index.html HTTP/1.1"
  haystack_accept = "POST /api/v1/checkout HTTP/1.1"

  # Fast reject without regex evaluation
  has_prefix_reject = prefix in haystack_reject
  assert not has_prefix_reject and re.search(pattern, haystack_reject) is None
  print(
      f"Haystack '{haystack_reject}': prefilter found ="
      f" {has_prefix_reject} -> Bypassed (0 automaton steps)"
  )

  has_prefix_accept = prefix in haystack_accept
  assert has_prefix_accept and re.search(pattern, haystack_accept) is not None
  print(
      f"Haystack '{haystack_accept}': prefilter found ="
      f" {has_prefix_accept} -> Dispatched to regex engine"
  )
  print()


def main() -> None:
  evaluate_polynomial_backtracking()
  evaluate_cloudflare_wildcards()
  evaluate_python_re_defenses()
  evaluate_literal_prefiltering()
  print("ALL CHECKS PASSED: Regex engine automata guarantees verified.")


if __name__ == "__main__":
  sys.exit(main())
