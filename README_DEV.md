# Developer Notes

## Nautobot Custom Field Updates in Jobs

When updating Device custom fields from an in-process Nautobot Job, do not treat the model object like the REST API payload.

The REST API accepts a payload shaped like:

```json
{
  "custom_fields": {
    "ai_resource_review": "..."
  }
}
```

Inside a Nautobot Job Hook Receiver, `changed_object` is a Django/Nautobot model instance. Depending on Nautobot version and model implementation, `device.custom_field_data` may be a read-only property without a setter. Code like this can fail:

```python
data = dict(device.custom_field_data or {})
data["ai_resource_review"] = review
device.custom_field_data = data
```

Observed failure:

```text
AttributeError: property 'custom_field_data' of 'Device' object has no setter
```

Prefer the model custom-field accessor when available:

```python
device.cf["ai_resource_review"] = review
```

If `cf` is not available, mutate an existing `custom_field_data` dictionary in place instead of assigning a new dictionary:

```python
data = getattr(device, "custom_field_data", None)
if isinstance(data, dict):
    data["ai_resource_review"] = review
```

The helper in `jobs/ai_resource_review.py` follows this order:

1. Use `device.cf[key] = value` when available.
2. Otherwise update an existing `device.custom_field_data` dictionary in place.
3. Raise an explicit error if neither writable path exists.

After changing custom fields on a Nautobot model instance, save with `validated_save()` when available so Nautobot validation still runs.

## Ollama Thinking Models

Some Ollama models can return reasoning in a separate `thinking` field and leave `response` empty. The AI Resource Review job stores only the final review text from `response`, not the reasoning trace.

The job sends `think=false` in its `/api/generate` payload so thinking-capable models produce the final answer directly. If a model still returns an empty response, check the Job logs for:

- `response_length`
- `thinking_length`
- `done_reason`
- `eval_count`
- `prompt_eval_count`

Use `AI_RESOURCE_REVIEW_LOG_PROMPT=true` temporarily when debugging prompt/model behavior. Do not leave it enabled unless the prompt content is acceptable in Job logs.

## Local Checks

Run syntax checks after editing Job code:

```bash
python3 -m py_compile jobs/*.py
```

Remove generated cache directories before committing:

```bash
rm -rf jobs/__pycache__
```
