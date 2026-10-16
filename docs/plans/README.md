# Plans

Written in order, each before its implementation. Module names changed in
[2026-10-16-module-structure.md](2026-10-16-module-structure.md); earlier plans use the old names:

| Old name | Now |
| --- | --- |
| `app/collection/` (extract, validate, prompts, engine) | `app/conversation/` |
| `app/collection/models` (booking document) | `app/booking/models` |
| `app/quoting/` engine, prompts | `app/booking/quoting`, `quoting_prompts` |
| `app/booking_request/` engine, prompts | `app/booking/creation`, `creation_prompts` |
| `app/quoting|booking_request|offers/provider` | `app/providers/protocols` |
| `app/*/fake*` | `app/providers/fake_*` |
| `app/operations/` | `app/providers/operations/` (resume: `app/events/resume`) |
| `app/workflow/` | `app/booking/machine`, `app/events/*`, `app/persistence/serialize` |
| `app/services/` | `app/persistence/`, `app/conversation/messages` |
| `app/auth.py` | `app/security/auth.py` |
| `app/conversation.py` (`handle_user_turn`) | `app/conversation/turn.py` |
