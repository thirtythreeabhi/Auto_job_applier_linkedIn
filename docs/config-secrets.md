# `config/secrets.py` — login and AI

Your LinkedIn credentials and the optional AI setup. Also available in the **Account** tab
of the control panel.

## LinkedIn login (optional)

| Setting | What it is |
|---|---|
| `username` | Your LinkedIn username/email |
| `password` | Your LinkedIn password |

**Both are optional.** If you leave them at their defaults, the tool logs in using the
browser's saved profile, or asks you to log in manually in the Chrome window it opens.

Nothing here leaves your computer. `config/secrets.py` and `user_config.json` stay on disk.

## AI setup (optional)

The **Account** tab has two independent AI configurations. `use_AI` controls the legacy AI
client; `local_ai_enabled` controls the local model used by the current job suitability and
application-answer flows. The local model is enabled in the shipped defaults, so AI is not
fully off by default even though `use_AI` is `False`.

The AI layer is provider-agnostic — it is built on **LangChain + LangGraph**, so one set of
options covers every provider.

| Setting | What it is |
|---|---|
| `use_AI` | Master switch. `True` or `False` |
| `ai_provider` | `"openai"`, `"gemini"`, or `"deepseek"`. Use `"openai"` for OpenAI **or any OpenAI-compatible server**, including local ones like [Ollama](https://ollama.com/), [LM Studio](https://lmstudio.ai/) and vLLM. `"deepseek"` also behaves like `"openai"`. `"gemini"` uses your Google API key and ignores `llm_api_url` |
| `llm_model` | The model name your provider offers. OpenAI: `"gpt-4o-mini"`, `"gpt-4o"`, `"gpt-5-mini"`. Local: `"llama-3.2-3b-instruct"`, `"qwen2.5:latest"`. Gemini: `"gemini-2.5-flash"`, `"gemini-2.5-pro"` |
| `llm_api_key` | Your provider's API key. For local servers any placeholder works — leave it as `"not-needed"` |
| `llm_api_url` | Base URL of the server. Used by the `"openai"` provider family only. OpenAI: `"https://api.openai.com/v1/"`. LM Studio: `"http://localhost:1234/v1/"`. Ollama: `"http://localhost:11434/v1/"`. DeepSeek: `"https://api.deepseek.com/v1"` |
| `llm_temperature` | Sampling temperature. Leave as `None` to use the model's own default — **some newer models only allow their default**. Set a number like `0` or `0.3` to override |

The local model has its own settings in the same Account tab:

| Setting | What it is |
|---|---|
| `local_ai_enabled` | Enables the local model client independently of `use_AI` |
| `local_ai_provider` | `"ollama"`, `"openai"` for an OpenAI-compatible endpoint, or `"gemini"` |
| `local_ai_model` | Model identifier, default `"qwen3-vl:4b-instruct-8k"` |
| `local_ai_api_url` | OpenAI-compatible API base URL; defaults to Ollama at `http://localhost:11434/v1/` |
| `local_ai_api_key` | API key for the endpoint; use `"not-needed"` for local Ollama |
| `local_ai_ollama_num_ctx` | Ollama-only context window in tokens, default `8192`; larger values need more memory |
| `local_ai_ollama_keep_alive` | Ollama-only time to retain the model after requests, default `5m`; `0` unloads it and `-1` keeps it loaded |

The control panel saves changes to `user_config.json`; those saved values override the
defaults in `config/secrets.py` when the bot starts.

The **Test connection** action sends a short local prompt to the selected Ollama model. It
uses the context and keep-alive values currently shown in the form and may take time while
Ollama loads the model. Ollama uses its native API for these requests because its
OpenAI-compatible endpoint does not accept per-request context-size settings.

What the AI actually uses to answer questions comes from `user_information_all` in
[`config/questions.py`](config-questions.md#experience-and-profile).

To be told when an AI API connection fails, set `showAiErrorAlerts = True` in
[`config/settings.py`](config-settings.md).

---

[← Back to docs index](README.md) · [Configuration overview](configuration.md)
