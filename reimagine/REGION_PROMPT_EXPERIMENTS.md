# Region Prompt Experiments

## Objective

Evaluate reliable structured region generation from Gemma 4 through llama.cpp,
including output format, retry behavior, thinking mode, and completion-token
budget. The reference benchmark contains 20 images under `input/`.

## Original YAML Implementation

- Region responses use YAML inside `<regions>...</regions>` tags.
- Parsing uses `yaml.safe_load`, followed by region validation.
- Formatting and validation failures get up to three prompt attempts.
- Corrective attempts include the parser error and a short prior response.
- Prior responses over 2,000 characters are omitted to avoid continuing a
  truncated analysis.
- Transient HTTP 500 responses from the OpenAI-compatible completion endpoint
  are retried once after one second.
- Symbolic-link directories are followed during deterministic image discovery,
  with directory identity tracking to prevent cycles.
- Interrupted folder-sharded runs can resume from completed checkpoints.
- `--llm-max-tokens` controls the OpenAI-compatible completion budget and now
  defaults to 16,384.
- `--llm-reasoning` controls llama.cpp reasoning per request and now defaults to
  `on`; both defaults remain explicitly overridable for experiments.

The thinking experiments were run by locally prepending `<|think|>` to the
prompt files. That local prompt change is intentionally not part of the recorded
implementation; the results below motivate making thinking an explicit option
instead.

## Schema Experiment Implementation

- Region responses are bare JSON objects constrained by
  `prompts/regions.schema.json`.
- The schema is attached to each llama.cpp chat-completion request as
  the native top-level `json_schema` field; no server startup schema is required.
- Local semantic validation still checks required descriptions, element count,
  literal text, and fully on-canvas boxes.

## Runs

### Schema-Constrained JSON, Non-Thinking 8k

- Output: `outputs/input-json-schema-regions-v1/`
- Log: `outputs/input-json-schema-regions-v1.log`
- Completion budget: 8,192
- Thinking: off
- Outcome: 20/20 in one run
- Calls: 22
- Rejected responses: 2
  - 1 JSON response truncated at the 8,192-token limit
  - 1 response containing narrated analysis instead of JSON at the 8,192-token
    limit
- Responses at the 8,192-token limit: 2
- Completions of at least 7,000 tokens: 3
- Median completion tokens: 5,190
- Elapsed time: 1,490 seconds
- Region counts: 2-5, with every saved plan passing local semantic validation
- Prompt-cache hits: 22/22 calls

This was faster than the improved YAML baseline and matched the thinking 16k
run's 22-call completion count while retaining the 8k budget. The schema
reliably constrained successful responses, but this Gemma/llama.cpp combination
still emitted long reasoning before the JSON content on two calls. Both failures
reached the completion limit and succeeded through the existing corrective retry.
The schema cannot enforce the semantic instruction that the first region be the
largest by area; 12 plans use a larger background or secondary box and require
visual review rather than automatic rejection.

### Schema-Constrained JSON, Thinking 16k

- Output: `outputs/input-json-schema-regions-thinking-16k/`
- Log: `outputs/input-json-schema-regions-thinking-16k.log`
- Completion budget: 16,384
- Thinking: on via llama.cpp's per-request `reasoning` option
- Outcome: 20/20 in one run
- Calls: 20
- Rejected responses: 0
- Responses at the 16,384-token limit: 0
- Responses over 8,192 tokens: 2
- Completions of at least 7,000 tokens: 3
- Median completion tokens: 3,856.5
- Elapsed time: 1,321 seconds
- Separate reasoning content appeared on 20/20 calls
- Region counts: 2-5, with every saved plan passing local semantic validation
- Prompt-cache hits: 19/20 calls

This was the most reliable and fastest schema run: every initial response
produced valid JSON and passed semantic validation. Despite the larger budget,
it used fewer total completion tokens than schema-constrained non-thinking 8k
(98,083 versus 110,251) and finished approximately 11% faster. Two responses
used more than 8,192 tokens, so the 16k ceiling prevented potential truncation.
Thirteen plans contain a background or secondary region larger by area than the
first subject region, confirming that this remains a semantic/visual concern
rather than a schema-format issue.

### YAML Baseline, Early Prompt and Retry Behavior

- Output: `outputs/input-yaml-regions-v2/`
- Log: `outputs/input-yaml-regions-v2.log`
- Completion budget: 8,192
- Thinking: off
- Initial outcome: 19/20; the missing item succeeded on resume
- Calls in initial run: 30
- Rejected responses: 11
  - 6 invalid YAML
  - 4 missing or truncated region blocks
  - 1 parsed value that was not an object
- Responses at the 8,192-token limit: 8
- Median completion tokens: 4,838
- Initial elapsed time: 2,050 seconds

This run exposed runaway narrated analysis and a correction loop in which a
large truncated response was fed back to the model. The retry prompt was then
changed to omit oversized responses and explicitly require starting over.

### YAML Baseline, Improved Retry Behavior

- Output: `outputs/input-yaml-regions-v3/`
- Log: `outputs/input-yaml-regions-v3.log`
- Completion budget: 8,192
- Thinking: off
- Outcome: 20/20 in one run
- Calls: 23
- Rejected responses: 3
  - 1 invalid YAML
  - 1 missing or truncated region block
  - 1 parsed value that was not an object
- Responses at the 8,192-token limit: 3
- Completions of at least 7,000 tokens: 7
- Median completion tokens: 5,218
- Elapsed time: 1,716 seconds
- One transient HTTP 500 recovered through the transport retry

This was the most reliable and efficient tested configuration.

### Thinking, 8k Budget

- Output: `outputs/input-yaml-regions-thinking/`
- Log: `outputs/input-yaml-regions-thinking.log`
- Completion budget: 8,192
- Thinking: on via `<|think|>`
- Outcome: 19/20; `sports/cycling.jpg` failed all three attempts
- Calls: 25
- Rejected responses: 6
  - 4 invalid YAML
  - 1 missing or truncated region block
  - 1 parsed value that was not an object
- Responses at the 8,192-token limit: 6
- Completions of at least 7,000 tokens: 12
- Median completion tokens: 6,832
- Recorded resumed-run elapsed time: 1,875 seconds
- Separate reasoning content appeared on 24 of 25 calls

Thinking improved some difficult semantic judgments, but doubled formatting
rejections, used more tokens, and failed one item. It also produced a one-region
plan for `animals/dog-leap.jpg`, revealing that validation currently permits
fewer regions than the prompt requests.

### Thinking, 16k Budget

- Output: `outputs/input-yaml-regions-thinking-16k/`
- Log: `outputs/input-yaml-regions-thinking-16k.log`
- Completion budget: 16,384
- Thinking: on via `<|think|>`
- Outcome: 20/20 in one run
- Calls: 22
- Rejected responses: 2
  - 1 missing or truncated region block
  - 1 response missing `high_level_description`
- Responses at the 16,384-token limit: 1
- Responses over 8,192 tokens: 5
- Completions of at least 7,000 tokens: 8
- Median completion tokens: 5,396
- Elapsed time: 2,200 seconds

The larger budget improved completion reliability relative to thinking at 8k:
all items completed, only two corrective attempts were needed, and
`sports/cycling.jpg` succeeded. It cost approximately 28% more elapsed time than
the non-thinking baseline. Laptop sleep may affect wall-clock measurements, so
the timing comparison is directional rather than a controlled benchmark.

## Comparison

| Configuration | Final output | Calls | Rejections | Budget hits | Median tokens | Recorded time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Schema JSON, thinking 16k | 20/20 | 20 | 0 | 0 at 16k | 3,856.5 | 1,321s |
| Schema JSON, non-thinking 8k | 20/20 | 22 | 2 | 2 at 8k | 5,190 | 1,490s |
| Non-thinking 8k, improved retry | 20/20 | 23 | 3 | 3 at 8k | 5,218 | 1,716s |
| Thinking 8k | 19/20 | 25 | 6 | 6 at 8k | 6,832 | 1,875s |
| Thinking 16k | 20/20 | 22 | 2 | 1 at 16k | 5,396 | 2,200s |

All completed 16k plans had populated required descriptive fields, valid
on-canvas boxes, and 2-5 elements. The non-thinking baseline also had no invalid
boxes or missing required fields.

## Visual Prompt Spot Checks

Six reference images were visually inspected against their non-thinking 8k and
thinking 8k plans. Selected 16k plans were then compared with the same images.

### Thinking Improvements

- `animals/dolphin-breach.jpg`: thinking 8k correctly described the dolphin's
  head as pointing left. Non-thinking 8k and thinking 16k said right.
- `everyday/painter.jpg`: thinking 8k correctly identified the painter as facing
  left toward the canvas and distinguished the live model from the painted
  Madonna. Non-thinking 8k and thinking 16k said the painter faced right.
- `sports/gymnastics.jpg`: thinking 8k gave a grounded tuck description and made
  the visible `116` bib a text region. Thinking 16k omitted the text region and
  used a broad background region instead.
- `animals/bird-takeoff.jpg`: both thinking plans improved color and posture
  language over the non-thinking plan, whose high-level description contained
  the erroneous phrase "mid-differentiation." Spatial boxes were effectively
  identical across configurations.

### Non-thinking Improvements

- `animals/dog-leap.jpg`: non-thinking detected the small blemish on the fabric.
  Thinking 8k omitted it and returned only one region. Thinking 16k restored it.
- `dance/contemporary.jpg`: non-thinking described the upper dancers and their
  clothing more accurately. Thinking 8k added a useful shadow region but made
  some costume and orientation errors. Thinking 16k omitted one upper dancer.

### Interpretation

- Thinking can improve difficult orientation and scene-relationship reasoning.
- More thinking budget does not monotonically improve semantic quality. The 16k
  run regressed the dolphin and painter orientation judgments that thinking 8k
  had corrected.
- Bounding-box quality was nearly identical across modes. Most corresponding
  coordinates differed by only 0.01-0.03.
- The 16k budget primarily improved completion reliability, not observed prompt
  fidelity.
- Sampling variance is a confounder because each configuration generated one
  sample per image. A stronger experiment should repeat each configuration with
  fixed or recorded seeds if llama.cpp exposes deterministic sampling controls.

## Conclusions

1. YAML plus corrective retries is workable, but free-form generation still
   wastes substantial compute on analysis and malformed syntax.
2. Non-thinking at 8k is currently the best default for reliability, speed, and
   format compliance.
3. Thinking is worth preserving as an experiment because it sometimes improves
   orientation and relationship reasoning.
4. Raising thinking from 8k to 16k recovers truncation-related failures but does
   not consistently improve semantic quality.
5. The next major experiment should constrain decoding with a JSON Schema rather
   than spend more tokens attempting to prompt the model into valid YAML.

## Manual Prompt Benchmark

The same 20-image set was run through `system_manual.txt` after removing its
explicit "Think first" instruction. Both modes used a 16,384-token ceiling and
the same local Gemma 4 / llama.cpp server.

| Configuration | Final output | Calls | Rejections | Completion tokens | Median item time | Recorded time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Manual, reasoning off | 20/20 | 20 | 0 | 45,393 | 29.62s | 658.35s |
| Manual, reasoning on | 20/20 | 22 | 2 | 76,957 | 38.17s | 1,143.65s |

- Reasoning off finished in 10m 58s, about 50% of the schema-JSON region
  reasoning run's 22m 01s and 44% of the region non-reasoning run's 24m 50s.
- Reasoning on finished in 19m 04s, 74% slower than reasoning off. One image
  (`everyday/chef.jpg`) consumed two rejected attempts, including one response
  that reached the 16,384-token ceiling, before succeeding on the third call.
- The model still returned separate `reasoning_content` on 19 of 20 requests
  with the request-level mode set to `off`. The flag changes model behavior and
  latency, but for this model/template it does not literally suppress all
  extracted reasoning content.
- The manual output contract is validated independently of the YAML manifest
  serialization. Region-generation YAML has been fully replaced by bare,
  schema-constrained JSON; `pipeline.yaml`, `render_state.yaml`, and gallery
  projection YAML remain intentional durable file formats.

## Next Session TODOs

- [x] Define a JSON Schema for the complete region response.
- [x] Require `high_level_description`, `background`, and `elements`.
- [x] Constrain `elements` to 2-6 entries and require `type`, `desc`, `x`, `y`,
      `w`, and `h` on each entry.
- [x] Constrain `type` to `obj` or `text`, with `text` conditionally required for
      text regions if supported by the schema implementation.
- [x] Constrain coordinates to 0-1 and dimensions to values greater than zero.
- [x] Constrain palettes to quoted six-digit hex colors.
- [x] Investigate the llama.cpp OpenAI-compatible option for schema-constrained
      JSON output, including the exact request field supported by the installed
      server version.
- [x] Add a structured-output transport option to `OpenAILLM` without affecting
      Claude Code or plain-text prompt generation.
- [x] Parse and validate schema-constrained JSON while retaining semantic
      validation for on-canvas boxes and usable descriptions.
- [x] Add an explicit experimental thinking flag instead of permanently adding
      `<|think|>` to prompt files.
- [ ] Run the same 20-image set with schema-constrained JSON in these modes:
      non-thinking 8k, thinking 8k, and thinking 16k.
- [ ] Compare completion success, retries, token use, latency, schema failures,
      required-field completeness, region counts, and coordinate validity.
- [ ] Visually spot-check the same benchmark images used here, especially
      `dolphin-breach`, `painter`, `dog-leap`, `contemporary`, and `gymnastics`.
- [ ] If practical, run multiple deterministic or seeded samples per image to
      separate thinking effects from sampling variance.
- [ ] Render matched winners when ComfyUI is available and compare actual image
      fidelity rather than relying only on prompt inspection.
