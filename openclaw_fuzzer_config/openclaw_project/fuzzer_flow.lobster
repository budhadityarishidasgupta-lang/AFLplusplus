version: "1"
name: local-fuzzer-flow
description: >-
  Deterministic, approval-gated workflow for the loopback mock target only.

args:
  target:
    type: object
    required: true
    properties:
      host:
        type: string
        const: "127.0.0.1"
      port:
        type: integer
        const: 3000
      protocol:
        type: string
        enum: [mock-bytes, mock-json]
      constraints:
        type: object
        required: [minimum_length, maximum_length, encoding]
        properties:
          minimum_length: {type: integer, minimum: 0}
          maximum_length: {type: integer, minimum: 1, maximum: 4096}
          encoding: {type: string, enum: [bytes, ascii, utf-8]}
  cycle_id:
    type: string
    required: true
    pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
  seed:
    type: integer
    required: false
    default: 1337
  max_generations:
    type: integer
    required: false
    default: 20
    minimum: 1
    maximum: 100

state:
  type: object
  schema:
    cycle_id: {type: string}
    target: {type: object}
    blueprint: {type: object}
    approval: {type: object}
    campaign: {type: object}
    assessment: {type: object}
    final_report: {type: object}
  initialize:
    cycle_id: "${args.cycle_id}"
    target: "${args.target}"
    blueprint: {}
    approval: {approved: false, resumeToken: null}
    campaign: {generation: 0, fitness_score: 0, feedback: {}}
    assessment: {status: pending}
    final_report: {}
    governance: {state: CLEAR, request_path: "./shared_data/governance/requests/Modification_Request.json", approval: null}

policy:
  deterministic: true
  max_concurrency: 1
  network_allowlist:
    - {host: "127.0.0.1", port: 3000}
  artifact_root: "./shared_data"
  deny_path_escape: true

steps:
  - id: recon_target_analysis
    type: agent
    agent: recon_planner_01
    needs: []
    input:
      type: object
      value:
        cycle_id: "${state.cycle_id}"
        target: "${state.target}"
        metadata_root: "./shared_data"
        output_path: "./shared_data/blueprint.json"
    output:
      type: object
      state_path: blueprint
      required: [schema_version, cycle_id, target, constraints, safety]

  - id: approve_blueprint
    type: approval
    needs: [recon_target_analysis]
    prompt: >-
      Verify that blueprint.json targets only 127.0.0.1:3000, contains no
      executable payload, and preserves all local-only safety flags.
    input:
      type: object
      value:
        cycle_id: "${state.cycle_id}"
        blueprint: "${state.blueprint}"
    pause:
      return: resumeToken
      state_path: approval.resumeToken
    resume:
      token: "${resumeToken}"
      decision_schema:
        type: object
        required: [approved, operator]
        properties:
          approved: {type: boolean}
          operator: {type: string}
          comment: {type: string}
      state_path: approval
    reject_transition: workflow_rejected

  - id: initial_fuzz_generation
    type: agent
    agent: exploit_dev_01
    needs: [approve_blueprint]
    when: "${state.approval.approved == true}"
    input:
      type: object
      value:
        cycle_id: "${state.cycle_id}"
        blueprint: "${state.blueprint}"
        seed: "${args.seed}"
        generation: 1
        prior_feedback: {}
    output:
      type: object
      state_path: campaign
      required: [generation, fitness_score, feedback]

  - id: evolve_until_threshold
    type: subworkflow
    needs: [initial_fuzz_generation]
    when: "${state.campaign.fitness_score < 100}"
    workflow:
      mode: bounded-loop
      max_iterations: "${args.max_generations - 1}"
      while: "${state.campaign.fitness_score < 100}"
      steps:
        - id: mutate_and_retry
          type: agent
          agent: exploit_dev_01
          input:
            type: object
            value:
              cycle_id: "${state.cycle_id}"
              blueprint: "${state.blueprint}"
              seed: "${args.seed}"
              generation: "${state.campaign.generation + 1}"
              prior_feedback: "${state.campaign.feedback}"
          output:
            type: object
            state_path: campaign
            required: [generation, fitness_score, feedback]
        - id: assess_fitness
          type: condition
          needs: [mutate_and_retry]
          expression: "${state.campaign.fitness_score >= 100}"
          on_true: break
          on_false: continue
    output:
      type: object
      state_path: assessment
      value:
        status: "${state.campaign.fitness_score >= 100 ? 'threshold_met' : 'budget_exhausted'}"
        fitness_score: "${state.campaign.fitness_score}"
        generation: "${state.campaign.generation}"

  - id: check_structural_changes
    type: condition
    needs: [initial_fuzz_generation, evolve_until_threshold]
    expression: "${file_exists(state.governance.request_path)}"
    on_true: system_architect_checkpoint
    on_false: normalize_assessment

  - id: system_architect_checkpoint
    type: approval
    agent: system_architect_01
    needs: [check_structural_changes]
    prompt: >-
      CRITICAL SYSTEM MODIFICATION DETECTED. Review the redacted
      Modification_Request.json and present: "CRITICAL SYSTEM MODIFICATION
      DETECTED. Do you authorize consulting Codex/Manus to build and commit
      this code change? [Yes/No]". Suspend all dependent execution until the
      operator answers for the exact request digest.
    input:
      type: object
      value:
        request_path: "${state.governance.request_path}"
        cycle_id: "${state.cycle_id}"
    pause:
      return: resumeToken
      state_path: governance.resumeToken
    resume:
      token: "${resumeToken}"
      decision_schema:
        type: object
        required: [approved, operator]
        properties:
          approved: {type: boolean}
          operator: {type: string}
          request_digest: {type: string}
          comment: {type: string}
      state_path: governance.approval
    reject_transition: governance_rejected

  - id: normalize_assessment
    type: condition
    needs: [check_structural_changes, system_architect_checkpoint]
    expression: "${state.campaign.fitness_score >= 100 && state.governance.approval.approved != false}"
    on_true: final_logging_and_patch
    on_false: budget_exhausted_log

  - id: final_logging_and_patch
    type: agent
    agent: recon_planner_01
    needs: [normalize_assessment]
    when: "${state.campaign.fitness_score >= 100}"
    input:
      type: object
      value:
        cycle_id: "${state.cycle_id}"
        blueprint: "${state.blueprint}"
        feedback: "${state.campaign.feedback}"
        fitness_score: "${state.campaign.fitness_score}"
        output_format: defensive-finding-and-patch-draft
        workflow_log: "./shared_data/WORKFLOW_STATUS.md"
    output:
      type: object
      state_path: final_report
      required: [finding_summary, defensive_patch_guidance, artifact_paths]

  - id: budget_exhausted_log
    type: ledger
    needs: [normalize_assessment]
    when: "${state.campaign.fitness_score < 100}"
    append: "./shared_data/WORKFLOW_STATUS.md"
    record:
      cycle_id: "${state.cycle_id}"
      state: COMPLETE_NO_THRESHOLD
      generation: "${state.campaign.generation}"
      fitness_score: "${state.campaign.fitness_score}"

  - id: governance_rejected
    type: ledger
    append: "./shared_data/WORKFLOW_STATUS.md"
    record:
      cycle_id: "${state.cycle_id}"
      state: STRUCTURAL_CHANGE_REJECTED
      reason: operator_denied_modification_request

  - id: workflow_rejected
    type: ledger
    append: "./shared_data/WORKFLOW_STATUS.md"
    record:
      cycle_id: "${state.cycle_id}"
      state: REJECTED
      reason: operator_denied_blueprint

outputs:
  type: object
  value:
    cycle_id: "${state.cycle_id}"
    approved: "${state.approval.approved}"
    fitness_score: "${state.campaign.fitness_score}"
    assessment: "${state.assessment}"
    final_report: "${state.final_report}"

