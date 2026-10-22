# avatars

Chainlit serves assistant / user avatars from `public/avatars/<id>.<ext>`; the
`/avatars/{id}` route only accepts `[a-zA-Z0-9_ .-]`, so the file name must be ASCII.

| File | Id used in `.chainlit/config.toml` | Source |
|---|---|---|
| `assistant.png` | `default_avatar_file_url = "/avatars/assistant"` | copy of `agents/bill/assets/logo.png` |

Why an ASCII id: the UI name is Chinese (`name = "物流业务助手"`), and Chainlit would
request `/avatars/物流业务助手`, which the route rejects with 400 (the avatar then renders
as a blank dot). Pointing `default_avatar_file_url` at an ASCII id avoids that entirely.

Keep the id ASCII; rename the file and the config value together.
