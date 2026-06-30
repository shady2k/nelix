# MIGRATION.md — fabricated observe() assertion migration table

Generated for Task 4 (nelix-5gc).  One row per test function in the two source files.
Verdict: **convert** (→ real frame + sidecar), **keep** (actuation / pure-helper / ask_mode / negative-only), **delete** (dead/dup), **superseded** (already covered by Tier-2 real-capture test).

## tests/test_driver_claude_observe.py

| function | verdict | invariant | source/notes |
|---------|---------|-----------|-------------|
| `test_working_spinner_is_busy` | convert | I3 | s-039a61b4-bg-subagent.raw --at-marker Dilly-dallying → working/spinner.txt |
| `test_working_heartbeat_fp_tracks_animation` | keep | — | pair test (two fabricated frames for semantic_fp stability); no single real frame replaces this pair comparison |
| `test_interrupt_marker_is_busy_and_affords_interrupt` | keep | — | interrupt affordance; no manifest entry; keep synthetic |
| `test_empty_prompt_is_free_text` | convert | I4a | s-b8a30317-delivery.raw --at-marker 'shift+tab to cycle' → idle_prompt/free-text-footer.txt |
| `test_stray_prompt_marker_without_footer_is_not_free_text` | convert | I4a | s-039a61b4-bg-subagent.raw scan for unknown prompt_kind → idle_prompt/bare-prompt.txt |
| `test_numbered_menu_is_modal_choice_with_options` | convert | I6a | s-6e9d8956 --at-marker '2. No, exit' → permission_prompt/trust-dialog.txt (modal_choice) |
| `test_yes_no_menu_is_permission_choice` | convert | I6a | existing permission_prompt/edit-menu.txt (real frame) + sidecar permission_prompt/edit-menu.yaml |
| `test_submitted_echo_detected` | keep | — | tests typed-text echo ("commit this"), not NBSP; I1 real frame covers NBSP paste only; keep typed-text synthetic |
| `test_no_echo_when_text_absent` | keep | — | negative echo test; no real frame for negative → keep synthetic |
| `test_echo_only_in_scrollback_is_not_present` | keep | — | scrollback non-detection + active detection pair; I4b Tier-2 sequence covers this later; keep synthetic |
| `test_crash_and_exit_from_ctx` | keep | — | crash/exit derived from ctx (not frame content); purely synthetic |
| `test_crash_banner_in_frame` | keep | — | crash banner; no manifest entry; keep synthetic |
| `test_ask_mode_reflected` | keep | I-AM | DEFERRED by scope: ask_mode is out-of-scope for this task |
| `test_fingerprints_split_content_from_input` | keep | — | content_fp stability pair test; no single real frame replaces the pair comparison |
| `test_busy_reason_from_chrome_only` | convert | I-BC + I3 | Bash-panel case → I-BC working/bash-panel.txt; plain-spinner case → I3 working/spinner.txt (busy_reason:null added to sidecar) |
| `test_format_submission_wraps_in_bracketed_paste` | keep | — | actuation method test; synthetic by design |
| `test_select_option_presses_digit_and_submits` | keep | — | actuation method test; synthetic by design |
| `test_submit_text_is_raw_answer` | keep | — | actuation method test; synthetic by design |
| `test_interrupt_is_escape` | keep | — | actuation method test; synthetic by design |
| `test_classify_and_folded_predicates_are_gone` | keep | — | structural/protocol check; synthetic by design |
| `test_running_background_subagent_is_busy_not_free_text` | convert | I2a | s-039a61b4-bg-subagent.raw --at-marker 'Waiting for' → background/bg-subagent.txt |
| `test_background_subagent_ticker_does_not_churn_semantic_fp` | keep | — | semantic_fp stability pair test (ticker counter zeroed); no single real frame replaces the pair comparison |
| `test_modal_prompt_during_background_subagent_still_surfaces` | keep | — | complex hybrid (permission_choice during bg subagent); no manifest entry; keep synthetic |

## tests/test_driver_claude.py

| function | verdict | invariant | source/notes |
|---------|---------|-----------|-------------|
| `test_normalize_frame_zeroes_spinner` | keep | — | tests normalize_frame() method directly, not observe(); keep |
| `test_format_submission_wraps_task_in_bracketed_paste` | keep | — | actuation method test; synthetic by design |
| `test_is_transcript_volatile_anchored` | keep | I5 | tests is_transcript_volatile() method on individual lines, not observe(); I5 real frame adds observe() coverage; this method test stays |
| `test_modal_menus_surface_as_choices_input_box_does_not` | convert | I6a + I4a | all 3 scenarios (modal_choice, permission_choice, free_text) now covered by real frames; delete |
| `test_input_box_is_only_free_text_not_a_menu` | convert | I6a + I4a | affordance checks for 3 scenarios all covered by real frame sidecars; delete |
| `test_submitted_echo_detects_typed_or_pasted_task` | keep | — | multi-scenario: typed text, pasted placeholder, scrollback; partially covered by I1 but function has synthetic parts; keep |
| `test_pasted_placeholder_only_on_active_input_line` | convert | I1 | s-b8a30317-delivery.raw --at-marker '[Pasted text' → delivery/nbsp-paste.txt; NBSP on active line; delete |

## Summary

- **convert**: 8 functions (→ 7 real frame fixtures across I1/I2a/I3/I4a/I5/I6a/I-BC)
- **keep**: 15 functions (actuation × 5, structural × 1, ask_mode-deferred × 1, pair/negative/hybrid × 8)
- **delete**: 0 (none found dead/dup)
- **superseded**: 0 (Tier-2 sequence tests not yet landed in this task)
