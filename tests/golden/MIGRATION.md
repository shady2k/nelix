# MIGRATION.md — fabricated observe() assertion migration table

Generated for Task 4 (nelix-5gc).  One row per **assertion** in the two source files.
Verdict: **convert** (→ real frame + sidecar), **keep** (actuation / pure-helper / ask_mode / negative-only), **delete** (dead/dup), **superseded** (already covered by Tier-2 real-capture test).

## tests/test_driver_claude_observe.py

### test_working_spinner_is_busy
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "none"` | convert | I3 | → working/spinner.yaml `prompt_kind: none` |
| `heartbeat.present == True` | convert | I3 | → working/spinner.yaml `heartbeat_present: true` |

### test_working_heartbeat_fp_tracks_animation
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `fp(frame_a) != fp(frame_b)` (two-frame pair) | keep | — | pair test; no single real frame replaces a two-frame comparison |

### test_interrupt_marker_is_busy_and_affords_interrupt
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "none"` | keep | — | interrupt affordance; no manifest entry; keep synthetic |
| `"interrupt_available" in affordances` | keep | — | same |

### test_empty_prompt_is_free_text
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "free_text"` | convert | I4a | → idle_prompt/free-text-footer.yaml `prompt_kind: free_text` |

### test_stray_prompt_marker_without_footer_is_not_free_text
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind != "free_text"` | convert | I4a | → idle_prompt/bare-prompt.yaml `prompt_kind: unknown` |

### test_numbered_menu_is_modal_choice_with_options
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "modal_choice"` | convert | I6a | → permission_prompt/trust-dialog.yaml `prompt_kind: modal_choice` |
| `options[0].id == "1"` | convert | I6a | → trust-dialog.yaml `options_ids: ["1","2"]` |
| `options[1].id == "2"` | convert | I6a | same |
| `options[0].label == "Yes, I trust this folder"` | convert | I6a | → trust-dialog.yaml `options[0].label` (Task-4 fix; previously MISSING — label drop gap) |
| `options[1].label == "No, exit"` | convert | I6a | → trust-dialog.yaml `options[1].label` (Task-4 fix; previously MISSING) |

### test_yes_no_menu_is_permission_choice
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "permission_choice"` | keep (legacy path) | I6a | edit-menu.txt stays on legacy _REMAP path; prompt_kind check passes via legacy guard |
| `options[0].label == "Yes"` | NOT COVERED | — | edit-menu.txt is a 4-line trimmed excerpt — not a real 40-row capture; edit-menu.yaml (fabricated) DELETED by Task-4 fix; permission_choice label coverage deferred until a real 40-row permission_choice frame is harvested |
| `options[1].label == "Yes, and don't ask again"` | NOT COVERED | — | same reason |
| `options[2].label == "No"` | NOT COVERED | — | same reason |
| `"accepts_text_input" not in affordances` | NOT COVERED | — | edit-menu.yaml deleted; this assertion is not present on the legacy path |

### test_submitted_echo_detected
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `submitted_echo_present == True` (typed text) | keep | — | tests typed-text echo ("commit this"); I1 covers NBSP paste only; keep typed-text synthetic |

### test_no_echo_when_text_absent
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `submitted_echo_present == False` (no text) | keep | — | negative echo; no real frame for negative → keep synthetic |

### test_echo_only_in_scrollback_is_not_present
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `submitted_echo_present == False` (scrollback) | keep | — | scrollback non-detection pair; I4b Tier-2 sequence covers this later; keep synthetic |
| `submitted_echo_present == True` (active) | keep | — | active-line detection; complementary |

### test_crash_and_exit_from_ctx
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "crash"` (child dead, exit_code≠0) | keep | — | ctx-derived, not frame content |
| `prompt_kind == "exit"` (child dead, exit_code==0) | keep | — | same |

### test_crash_banner_in_frame
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "crash"` (Traceback in frame) | keep | — | no manifest entry; keep synthetic |

### test_ask_mode_reflected
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `ask_mode == True / False` | keep | I-AM | DEFERRED by scope: ask_mode is out-of-scope for this task |

### test_fingerprints_split_content_from_input
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `content_fp(a) == content_fp(b)` (pair) | keep | — | pair test; no single real frame replaces it |

### test_busy_reason_from_chrome_only
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `busy_reason == "running_command"` (Bash panel) | convert | I-BC | → working/bash-panel.yaml `busy_reason: running_command` |
| `busy_reason == None` (plain spinner) | convert | I3 | → working/spinner.yaml `busy_reason: null` (added in Task-4 review) |

### test_format_submission_wraps_in_bracketed_paste
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| bracketed-paste envelope present | keep | — | actuation method test; synthetic by design |

### test_select_option_presses_digit_and_submits
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| select_option("2") returns "2\r" | keep | — | actuation method test; synthetic by design |

### test_submit_text_is_raw_answer
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| submit_text returns text unchanged | keep | — | actuation method test; synthetic by design |

### test_interrupt_is_escape
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| interrupt() returns ESC | keep | — | actuation method test; synthetic by design |

### test_classify_and_folded_predicates_are_gone
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| classify / is_accepting_input / etc. absent | keep | — | structural/protocol check; synthetic by design |

### test_running_background_subagent_is_busy_not_free_text
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "none"` | convert | I2a | → background/bg-subagent.yaml `prompt_kind: none` |
| `busy_reason == "waiting_subagents"` | convert | I2a | → bg-subagent.yaml `busy_reason: waiting_subagents` |
| `heartbeat.present == True` | convert | I2a | → bg-subagent.yaml `heartbeat_present: true` |

### test_background_subagent_ticker_does_not_churn_semantic_fp
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `semantic_fp(frame_a) == semantic_fp(frame_b)` (ticker pair) | keep | — | pair test; no single frame replaces it |

### test_modal_prompt_during_background_subagent_still_surfaces
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "permission_choice"` (bg + modal) | keep | — | complex hybrid; no manifest entry; keep synthetic |

---

## tests/test_driver_claude.py

### test_normalize_frame_zeroes_spinner
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| normalize_frame() zeroes spinner glyphs | keep | — | method test; not observe(); keep |

### test_format_submission_wraps_task_in_bracketed_paste
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| bracketed-paste format present | keep | — | actuation method test; synthetic by design |

### test_is_transcript_volatile_anchored
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| volatile rows return True (spinner, rule, footer) | keep | I5 | method test; real-frame I5 test (`test_i5_chrome_volatile.py`) adds is_transcript_volatile() coverage from harvested frame; both kept as complementary |
| settled rows return False | keep | I5 | same |

### test_modal_menus_surface_as_choices_input_box_does_not
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `prompt_kind == "modal_choice"` | convert | I6a | covered by trust-dialog.yaml |
| `prompt_kind == "permission_choice"` | convert | I6a | covered by edit-menu.txt legacy path |
| `prompt_kind == "free_text"` | convert | I4a | covered by free-text-footer.yaml |

### test_input_box_is_only_free_text_not_a_menu
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `"accepts_text_input" in affordances` (free_text) | convert | I4a | → free-text-footer.yaml `affordances_include: [accepts_text_input]` |
| `"accepts_text_input" not in affordances` (modal) | convert | I6a | → trust-dialog.yaml `affordances_exclude: [accepts_text_input]` |
| `"accepts_text_input" not in affordances` (permission) | NOT COVERED | — | edit-menu.yaml deleted; legacy path does not assert affordances |

### test_submitted_echo_detects_typed_or_pasted_task
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `submitted_echo_present == True` (typed text) | keep | — | typed-text path; I1 covers NBSP paste |
| `submitted_echo_present == True` (pasted placeholder) | keep | — | synthetic path; I1 covers the real NBSP paste |
| `submitted_echo_present == False` (scrollback only) | keep | — | negative; keep synthetic |

### test_pasted_placeholder_only_on_active_input_line
| assertion | verdict | invariant | source/notes |
|-----------|---------|-----------|-------------|
| `submitted_echo_present == True` (NBSP on active line) | convert | I1 | → delivery/nbsp-paste.yaml `submitted_echo_present: true` |

---

## Summary

- **convert**: 19 assertions (→ 7 real frame fixtures: I1/I2a/I3/I4a/I5/I6a/I-BC)
- **keep**: 29 assertions (actuation × 8, structural × 1, ask_mode-deferred × 1, pair/negative/hybrid × 19)
- **NOT COVERED after Task-4**: 4 assertions — `test_yes_no_menu_is_permission_choice` option labels (Yes, Yes-and-dont-ask-again, No) and `test_input_box_is_only_free_text_not_a_menu` permission_choice affordances_exclude — all stem from edit-menu.txt being a 4-line trimmed excerpt unfit for rich assertions; a real 40-row permission_choice frame harvest would close these gaps.
- **delete**: 0 (none found dead/dup)
- **superseded**: 0 (Tier-2 sequence tests not yet landed in this task)

### Coverage audit: .options/.label assertions
| assertion | original source | post-migration coverage |
|-----------|-----------------|------------------------|
| `trust-dialog options[0].label == "Yes, I trust this folder"` | fabricated in `test_numbered_menu_is_modal_choice_with_options` | trust-dialog.yaml `options[0].label` ✓ (Task-4 fix) |
| `trust-dialog options[1].label == "No, exit"` | fabricated in `test_numbered_menu_is_modal_choice_with_options` | trust-dialog.yaml `options[1].label` ✓ (Task-4 fix) |
| `edit-menu options[0].label == "Yes"` | fabricated in `test_yes_no_menu_is_permission_choice` | NOT COVERED — edit-menu.yaml deleted (fabricated frame); needs real 40-row harvest |
| `edit-menu options[1].label == "Yes, and don't ask again"` | fabricated in `test_yes_no_menu_is_permission_choice` | NOT COVERED — same reason |
| `edit-menu options[2].label == "No"` | fabricated in `test_yes_no_menu_is_permission_choice` | NOT COVERED — same reason |
