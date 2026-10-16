"""Conversation: messages, detail extraction, validation and follow-up questions.

turn      `handle_user_turn`: one user message through collection, quoting, creation, offers
engine    detail collection: extract, merge, validate, ask for the next missing field
extract   the `Extractor` interface and the deterministic rule-based extractor
validate  per-field validation rules
prompts   greeting, follow-up and completion wording
messages  assistant message construction and session guards

A language model, if added, belongs here and may only return extracted details or a
proposed next action as data; side effects stay in `booking`, `offers` and `providers`.
"""
