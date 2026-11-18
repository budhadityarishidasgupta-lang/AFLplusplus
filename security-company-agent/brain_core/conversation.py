"""Local conversational sessions and deterministic intent routing."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from . import studio


@dataclass(frozen=True)
class Route:
    intent: str
    confidence: str
    reason: str


def _has_negated_training_intent(folded: str) -> bool:
    """Detect explicit denials of lesson creation before considering training cues."""
    return bool(re.search(
        r'\b(?:do not|don.t|never|not|without)\s+(?:create|make|write|teach|train|save|modify|add)\b'
        r'|\bnot\s+(?:a\s+)?(?:training|lesson|memory)\s+(?:request|submission|update)\b'
        r'|\b(?:no|without)\s+(?:new\s+)?(?:training|lesson|memory)\b', folded))


def classify_intent(text: str) -> Route:
    """Route explicit user goals before structured fields or durable draft state."""
    body = (text or '').strip()
    folded = body.casefold()
    negated_training = _has_negated_training_intent(folded)
    mission_status_request = re.search(
        r'\b(?:what is your plan|show (?:me )?(?:the )?plan|what tasks require approval|'
        r'which tasks require approval|what have (?:your|the) agents completed|'
        r'what remains unfinished|what is unfinished|which agent can help me accomplish this)\b', folded)
    if mission_status_request:
        return Route('mission_status', 'explicit', 'Read-only request for the current mission plan or status.')

    # A clearly requested analysis or read-only recall is never a training submission,
    # even if the request mentions memory, lesson labels, or a pending training workflow.
    analysis = bool(re.search(
        r'\b(analy[sz]e|analysis|interpret|reason(?:ing)?|what should i do|how should i|'
        r'how do i respond|what should i say|help me decide|should i|thoughtful response|'
        r'what would be a thoughtful|how should i interpret this situation)\b', folded))
    starts_teaching_workflow = bool(re.search(
        r'\b(i want to teach|brain,? i want to teach|teach you something)\b', folded)) and not negated_training
    explicit_analysis_request = bool(re.search(
        r'\b(analy[sz]e|analysis|interpret|reason(?:ing)?|how should i|what should i|'
        r'how do i respond|what should i say|help me decide|should i|thoughtful response)\b', folded))
    explicit_analysis_request = explicit_analysis_request and not starts_teaching_workflow
    lesson_question_field = bool(re.search(
        r'(?im)^\s*(?:scenario summary|emotional context|observed facts|assumptions(?: or uncertainties)?|'
        r'corrective principle|recommended response pattern)\s*:', body))
    fields = extract_lesson_fields(body)
    if lesson_question_field:
        # These labels are content in a structured submission, not routing cues.
        explicit_analysis_request = bool(re.search(
            r'\b(analy[sz]e|analysis|interpret|reason(?:ing)?)\b', folded)) and not starts_teaching_workflow
    # The teaching lead-in only applies to the message that starts the workflow;
    # later labeled submissions remain training even if lesson content contains
    # a question-like phrase.
    if re.search(r'\b(?:acknowledge|understood|got it)\b.*\b(?:capability|agent)\b|\b(?:acknowledge|confirm) (?:the )?(?:latest )?(?:capability )?update\b', folded):
        return Route('capability_acknowledgement', 'explicit', 'Explicit acknowledgement of a capability update.')
    if re.search(
            r'\b(?:what changed|latest capability update|latest agent update|capability update|'
            r'what changed in your capabilities|what capabilities changed)\b', folded):
        return Route('capability_update', 'explicit', 'Read-only query about a detected capability update.')
    if re.search(r'\b(?:which agents remain unavailable|which agents are unavailable|what agents are unavailable|unavailable agents)\b', folded):
        return Route('agent_awareness', 'explicit', 'Read-only question about unavailable agents.')
    awareness = bool(re.search(
        r'\b(which agents do you know|what agents do you know|what agents are there|list (?:the )?agents|'
        r'what agents have you identified|what have you learned about (?:your|my|the) agent(?:s)?\b|'
        r'what do you understand about (?:your|my) agents|'
        r'tell me what agents you know|'
        r'what (?:can|capabilities can) (?:my )?agents do|what are (?:my )?agents capable of|'
        r'what can my agents do|can you command|which agents remain unavailable|which agents are unavailable|'
        r'what agents are unavailable|unavailable agents|which capabilities remain unavailable|what capabilities remain unavailable|'
        r'command (?:the )?.*agents|run (?:the )?.*agents|execute (?:the )?.*agents|'
        r'which capabilities are missing|what capabilities are missing|what capabilities are still missing|'
        r'which agents or capabilities are missing|what agents or capabilities are missing|'
        r'missing capabilities|what is missing)\b', folded))
    if awareness and not negated_training:
        return Route('agent_awareness', 'explicit', 'Read-only question about discovered agent capabilities.')
    # Explicit objectives are distinguished from factual questions before the
    # general fallback. Mission parsing itself remains a separate service.
    from .mission_intelligence import classify_mission_intent
    mission_kind = classify_mission_intent(body)
    explicit_goal = bool(re.search(
        r'\b(please|i need|i want|help me|my goal|my objective|plan|design|organize|prepare|build|create|develop|implement|migrate|upgrade|set up|configure|diagnose|troubleshoot|audit|assess)\b',
        folded) or re.match(
            r'^(?:research|investigate|compare|evaluate|recommend|find|identify|look up)\b', folded))
    research_in_situational_context = bool(
        re.search(r'\b(current|currently|recent|recently|latest|news|research|source|sources|evidence|study|studies|statistics|policy|policies|law|laws|regulation|regulations)\b', folded)
        and re.search(r'\b(i feel|i am worried|i\'m worried|my friend|my colleague|conversation)\b', folded))
    action_planning_language = bool(re.search(r'\b(plan|draft|prepare|organize)\b', folded))
    non_explicit_information_request = mission_kind == 'information_gathering' and not re.search(
        r'\b(please|i need|i want|help me|my goal|my objective|research|investigate|compare|evaluate|recommend|find|identify|look up)\b',
        folded)
    if (mission_kind != 'ordinary' and explicit_goal and not research_in_situational_context
            and (not non_explicit_information_request or action_planning_language)
            and not re.search(r'\bdo not\b', folded)
            and not re.search(
                r'\b(approved lessons|what do you remember|recall|i want to teach|teach you|training lesson|'
                r'do not|don.t|dont|never)\b', folded)
            and not lesson_question_field and not fields and not explicit_analysis_request):
        return Route('mission', 'explicit', f'Explicit {mission_kind.replace("_", " ")} objective.')

    recall = bool(re.search(
        r'\b(what do you remember|search approved memory|recall|what did i teach you|'
        r'what have i taught you|show (my )?approved lessons|retrieve (approved )?memory|'
        r'find (my )?(approved )?lesson|search (my )?approved lessons?)\b', folded))
    if recall and not analysis:
        return Route('memory_recall', 'explicit', 'Explicit read-only approved-memory retrieval request.')

    if not explicit_analysis_request and re.search(r'\b(approve|accept) (this )?(lesson|proposal)\b', folded):
        return Route('lesson_approval', 'explicit', 'Explicit approval of the current proposal.')
    if not explicit_analysis_request and re.search(r'\b(reject|decline|discard) (this )?(lesson|proposal)\b', folded):
        return Route('lesson_rejection', 'explicit', 'Explicit rejection of the current proposal.')
    if not explicit_analysis_request and re.search(r'\b(cancel|discard|abandon) (my |this |the )?(training|lesson|draft|proposal)\b', folded):
        return Route('training_cancel', 'explicit', 'Explicit request to cancel the current training draft.')
    if not explicit_analysis_request and re.search(r'\b(resume|continue) (my |this |the )?(training|lesson|draft|proposal)\b', folded):
        return Route('training_resume', 'explicit', 'Explicit request to resume the current training draft.')

    explicit_structured_submission = bool(re.search(
        r'\b(submit as a lesson|structured lesson submission|lesson fields submission)\b', folded))
    explicit_readonly_analysis = explicit_analysis_request and (
        negated_training or (not explicit_structured_submission and not fields and not lesson_question_field))
    if explicit_readonly_analysis:
        return Route('emotional_reasoning', 'explicit', 'Explicit request to analyze or interpret a situation.')
    if explicit_analysis_request and fields and not negated_training and not lesson_question_field:
        return Route('emotional_reasoning', 'explicit', 'Explicit analysis request takes precedence over embedded lesson fields.')
    if explicit_analysis_request and explicit_structured_submission and extract_lesson_fields(body) and not negated_training:
        return Route('training', 'structured', 'User explicitly identified the labeled text as a structured lesson submission.')
    if negated_training and not re.search(r'\b(cancel|discard|abandon) (my |this |the )?(training|lesson|draft|proposal)\b', folded):
        return Route('emotional_reasoning', 'explicit', 'Training or memory changes were explicitly disallowed.')

    if re.search(r'\b(you were wrong|your interpretation was wrong|correction:|correct your|that was incorrect|you misunderstood)\b', folded):
        return Route('correction', 'explicit', 'Explicit correction wording.')
    # A field block following an explicit teaching request starts a training draft;
    # otherwise require the complete six-field record unless the text clearly isn't analysis.
    explicit_teaching = bool(re.search(r'\b(i want to teach|teach you|teach a lesson|create a lesson|submit a lesson|train you|training lesson|lesson proposal)\b', folded)) and not negated_training
    if fields and not explicit_readonly_analysis and not negated_training:
        return Route('training', 'structured', 'Recognized labeled lesson fields were supplied for supervised training.')
    if explicit_teaching and not explicit_readonly_analysis:
        return Route('training', 'explicit', 'Explicit positive teaching intent.')
    if re.search(r'\b(what (do|did) you remember|what (was|is) the lesson about|tell me about (the )?(approved )?lesson)\b', folded):
        return Route('memory_recall', 'likely', 'Question asks about previously approved memory.')
    if re.search(r'\b(search|find|look up|retrieve) (approved )?(memory|lesson|lessons)\b', folded):
        return Route('memory_recall', 'likely', 'Explicit read-only memory search request.')
    if re.search(r'\b(what happened|someone .* stopped responding|stopped responding|feels? hurt|i feel|they feel|uncertain about|interpret this|my friend|my partner|my colleague|my relationship|does that mean they|do they still care|why did they)\b', folded):
        return Route('emotional_reasoning', 'likely', 'Emotional context or interpretation request.')
    if len(body.split()) <= 15 and re.search(r'\b(hello|hi|hey|good morning|good evening|how are you|thank you|thanks|who are you)\b', folded):
        return Route('general', 'likely', 'Short greeting or identity question.')

    # Broad cue words only route directly when the wording has no interpersonal or emotional context.
    direct_information_question = re.search(
        r'\b(?:explain|define|describe|what is|what are|how does|how do|why is|why are|when did|where is|who was)\b', folded)
    emotional_context = re.search(
        r'\b(?:i feel|i am worried|i am concerned|i am anxious|i am upset|i am confused|'
        r'my friend|my partner|my colleague|someone .* stopped responding|stopped responding|'
        r'feels? hurt|uncertain about|interpret this|conversation concern|conversation)\b', folded)
    current_information_question = re.search(
        r'\b(?:what|who|when|where|which|how|why)\b[^?!.]{0,60}\b(?:latest|current|recent)\b', folded)
    if (direct_information_question or current_information_question) and not emotional_context:
        return Route('general', 'likely', 'Informational question or current-information request uses general answering.')
    return Route('emotional_reasoning', 'default', 'Unclassified content uses the grounded reasoning route.')


_FIELD_MAP = {
    'scenario summary': 'scenario_summary', 'scenario': 'scenario_summary',
    'emotional context': 'emotional_context', 'emotions': 'emotional_context',
    'observed facts': 'observed_facts', 'facts': 'observed_facts',
    'assumptions or uncertainties': 'assumptions_or_uncertainties',
    'assumptions/uncertainties': 'assumptions_or_uncertainties',
    'uncertainties': 'assumptions_or_uncertainties', 'assumptions': 'assumptions_or_uncertainties',
    'corrective principle': 'corrective_principle', 'principle': 'corrective_principle',
    'recommended response pattern': 'recommended_response_pattern',
    'response pattern': 'recommended_response_pattern',
    'relationship context': 'relationship_context', 'relationship': 'relationship_context',
}


def extract_lesson_fields(text):
    """Parse only recognized labeled fields, preserving their supplied text."""
    fields, current = {}, None
    # Accept a field label at the beginning of a line or after a sentence.
    # A new recognized label ends the prior value; unknown colon text is retained.
    labels = '|'.join(re.escape(label) for label in sorted(_FIELD_MAP, key=len, reverse=True))
    marker = re.compile(rf'(?i)(?<![\w/])({labels})\s*:')
    for line in (text or '').splitlines():
        matches = list(marker.finditer(line))
        if not matches:
            if current and line.strip():
                fields[current].append(line.strip())
            continue
        prefix = line[:matches[0].start()].strip()
        if prefix and current:
            fields[current].append(prefix)
        for index, match in enumerate(matches):
            key = _FIELD_MAP[match.group(1).casefold()]
            fields.setdefault(key, [])
            current = key
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            value = line[start:end].strip()
            if value:
                fields[key].append(value)
    return {key: '\n'.join(values).strip() for key, values in fields.items()}


def lesson_fields_complete(fields):
    required = ('scenario_summary', 'emotional_context', 'observed_facts',
                'assumptions_or_uncertainties', 'corrective_principle',
                'recommended_response_pattern')
    return all(fields.get(key, '').strip() for key in required)


def missing_lesson_fields(fields):
    labels = {
        'scenario_summary': 'Scenario summary',
        'emotional_context': 'Emotional context',
        'observed_facts': 'Observed facts',
        'assumptions_or_uncertainties': 'Assumptions or uncertainties',
        'corrective_principle': 'Corrective principle',
        'recommended_response_pattern': 'Recommended response pattern',
    }
    return [label for key, label in labels.items() if not fields.get(key, '').strip()]


def prepare_lesson(text, *, existing=None):
    from . import lessons
    fields = dict(existing or {})
    fields.update(extract_lesson_fields(text))
    if not lesson_fields_complete(fields):
        return None, fields
    proposal = lessons.propose(
        scenario=fields['scenario_summary'], corrective_principle=fields['corrective_principle'],
        emotional_context=fields['emotional_context'], observed_facts=fields['observed_facts'],
        uncertainties=fields['assumptions_or_uncertainties'],
        response_pattern=fields['recommended_response_pattern'],
        relationship_context=fields.get('relationship_context', ''))
    return proposal, fields


def _ensure_chat_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_messages (
        message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
        created_at TEXT NOT NULL, content TEXT NOT NULL,
        message_type TEXT NOT NULL, related_lesson_id TEXT,
        inference_status TEXT NOT NULL DEFAULT 'not_applicable',
        FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE)''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_messages_conversation ON conversation_messages(conversation_id,created_at)')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_lesson_state (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
        state_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_mission_state (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
        state_json TEXT NOT NULL, request_key TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def create_session(title='New conversation', *, data_dir=None, db_path=None):
    studio.initialize_store(data_dir=data_dir, db_path=db_path)
    session_id = str(uuid.uuid4())
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        _ensure_chat_schema(db)
        now = studio.now()
        db.execute('INSERT INTO conversations(conversation_id,title,created_at,updated_at) VALUES(?,?,?,?)',
                   (session_id, (title or 'New conversation')[:120], now, now))
    return session_id


def _target(data_dir, db_path):
    return studio.Path(db_path) if db_path is not None else (studio.Path(data_dir) / 'brain.sqlite3' if data_dir is not None else studio.DB)


def save_lesson_state(conversation_id, state, *, data_dir=None, db_path=None):
    """Persist only the reviewable proposal/workflow state in the shared studio DB."""
    if state is not None and not isinstance(state, dict):
        raise ValueError('Lesson state must be a mapping or None.')
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        _ensure_chat_schema(db)
        if not db.execute('SELECT 1 FROM conversations WHERE conversation_id=?', (conversation_id,)).fetchone():
            raise ValueError('Conversation does not exist.')
        if state is None:
            db.execute('DELETE FROM conversation_lesson_state WHERE conversation_id=?', (conversation_id,))
        else:
            db.execute('''INSERT INTO conversation_lesson_state(conversation_id,state_json,updated_at)
                          VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                          state_json=excluded.state_json,updated_at=excluded.updated_at''',
                       (conversation_id, studio.json.dumps(state, ensure_ascii=False), studio.now()))


def load_lesson_state(conversation_id, *, data_dir=None, db_path=None):
    if not _target(data_dir, db_path).exists():
        return None
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        _ensure_chat_schema(db)
        row = db.execute('SELECT state_json FROM conversation_lesson_state WHERE conversation_id=?',
                         (conversation_id,)).fetchone()
    return studio.json.loads(row[0]) if row else None


def save_mission_state(conversation_id, state, request_key, *, data_dir=None, db_path=None):
    """Store one validated mission state, bound to its originating conversation."""
    if not isinstance(state, dict) or not isinstance(request_key, str) or not request_key:
        raise ValueError('Mission state and request key are required.')
    if state.get('_trusted_acceptance_harness') is not True:
        state = dict(state)
        state.pop('_trusted_acceptance_harness', None)
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        _ensure_chat_schema(db)
        if not db.execute('SELECT 1 FROM conversations WHERE conversation_id=?', (conversation_id,)).fetchone():
            raise ValueError('Conversation does not exist.')
        db.execute('''INSERT OR IGNORE INTO conversation_mission_state
                      (conversation_id,state_json,request_key,updated_at) VALUES(?,?,?,?)''',
                   (conversation_id, studio.json.dumps(state, ensure_ascii=False), request_key, studio.now()))
        row = db.execute('SELECT state_json,request_key FROM conversation_mission_state WHERE conversation_id=?',
                         (conversation_id,)).fetchone()
    return studio.json.loads(row[0]), row[1], row[1] == request_key


def persist_planning_approval(conversation_id, state, request_key, *, data_dir=None, db_path=None):
    """Persist only task-bound planning decisions; never execution authority."""
    import json
    if not isinstance(state, dict) or not isinstance(state.get('approvals'), dict):
        return False
    if data_dir is None and db_path is None:
        data_dir, db_path = studio.DATA, studio.DB
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        row = db.execute('SELECT state_json,request_key FROM conversation_mission_state WHERE conversation_id=?',
                         (conversation_id,)).fetchone()
        if not row or row[1] != request_key:
            return False
        current = studio.json.loads(row[0])
        tasks = current.get('plan', {}).get('tasks') or current.get('plan', {}).get('ordered_tasks', [])
        valid_ids = {task.get('task_id') for task in tasks if isinstance(task, dict)}
        state_synthetic = (state.get('synthetic_acceptance') is True
                           or state.get('plan', {}).get('synthetic_acceptance') is True)
        trusted_context = state.get('_trusted_acceptance_harness') is True
        if ((current.get('synthetic_acceptance') is True or current.get('plan', {}).get('synthetic_acceptance') is True)
                and state_synthetic and trusted_context):
            state_tasks = state.get('plan', {}).get('tasks') or state.get('plan', {}).get('ordered_tasks', [])
            state_ids = {task.get('task_id') for task in state_tasks if isinstance(task, dict)}
            if state_ids and all(str(item).rsplit('.', 1)[-1] in
                                 {str(value).rsplit('.', 1)[-1] for value in valid_ids} for item in state_ids):
                valid_ids = state_ids
        approvals = state['approvals']
        if state_synthetic:
            if not trusted_context or not (current.get('synthetic_acceptance') is True
                    or current.get('plan', {}).get('synthetic_acceptance') is True):
                return False
            state['synthetic_acceptance'] = True
            state['plan']['synthetic_acceptance'] = True
        synthetic_compat = (state_synthetic
                            and bool(approvals)
                            and all(str(task_id).rsplit('.', 1)[-1] in
                                    {str(valid_id).rsplit('.', 1)[-1] for valid_id in valid_ids}
                                    for task_id in approvals))
        if any((task_id not in valid_ids and not synthetic_compat) or not isinstance(item, dict)
                or set(item) != {'request', 'decision'}
                or not isinstance(item.get('request'), dict)
                or (item.get('decision') is not None and not isinstance(item.get('decision'), dict))
                for task_id, item in approvals.items()):
            return False
        old_approvals = current.get('approvals', {})
        if synthetic_compat:
            stored_tasks = current.get('plan', {}).get('tasks') or current.get('plan', {}).get('ordered_tasks', [])
            stored_by_suffix = {str(task.get('task_id', '')).rsplit('.', 1)[-1]: task.get('task_id')
                                for task in stored_tasks if isinstance(task, dict)}
            approvals = {stored_by_suffix.get(str(task_id).rsplit('.', 1)[-1], task_id): item
                         for task_id, item in approvals.items()}
            state['approvals'] = approvals
        if any(task_id in old_approvals and old_approvals[task_id] != item
               and (old_approvals[task_id].get('decision') is not None
                    or old_approvals[task_id].get('request') != item.get('request'))
               for task_id, item in approvals.items()):
            return False
        updated = dict(current)
        updated['approvals'] = {**old_approvals, **approvals}
        if synthetic_compat:
            stored_tasks = current.get('plan', {}).get('tasks') or current.get('plan', {}).get('ordered_tasks', [])
            task_ids_by_suffix = {str(task.get('task_id', '')).rsplit('.', 1)[-1]: task.get('task_id')
                                  for task in stored_tasks if isinstance(task, dict)}
            updated['approvals'] = {task_ids_by_suffix.get(str(task_id).rsplit('.', 1)[-1], task_id): item
                                    for task_id, item in updated['approvals'].items()}
        state['approvals'] = updated['approvals']
        encoded = json.dumps(updated, ensure_ascii=False)
        changed = db.execute(
            'UPDATE conversation_mission_state SET state_json=?,updated_at=? '
            'WHERE conversation_id=? AND request_key=? AND state_json=?',
            (encoded, studio.now(), conversation_id, request_key, row[0])).rowcount
        if changed != 1:
            return False
        saved = db.execute('SELECT state_json FROM conversation_mission_state WHERE conversation_id=? AND request_key=?',
                           (conversation_id, request_key)).fetchone()
        return bool(saved and studio.json.loads(saved[0]).get('approvals') == updated['approvals'])


def save_mission_state(conversation_id, state, request_key, *, data_dir=None, db_path=None):
    """Insert a mission once and persist only validated execution transitions."""
    if not isinstance(state, dict) or not isinstance(request_key, str) or not request_key:
        raise ValueError('Mission state and request key are required.')
    if data_dir is None and db_path is None:
        data_dir, db_path = studio.DATA, studio.DB
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        _ensure_chat_schema(db)
        if not db.execute('SELECT 1 FROM conversations WHERE conversation_id=?', (conversation_id,)).fetchone():
            raise ValueError('Conversation does not exist.')
        db.execute('INSERT OR IGNORE INTO conversation_mission_state(conversation_id,state_json,request_key,updated_at) VALUES(?,?,?,?)',
                   (conversation_id, studio.json.dumps(state, ensure_ascii=False), request_key, studio.now()))
        row = db.execute('SELECT state_json,request_key FROM conversation_mission_state WHERE conversation_id=?',
                         (conversation_id,)).fetchone()
        current, stored_key = studio.json.loads(row[0]), row[1]
        if stored_key != request_key:
            return current, stored_key, False
        if current.get('mission') != state.get('mission'):
            # Insert-once replay is idempotent: the original stored payload wins.
            # A different request key is the explicit conflict signal.
            return current, stored_key, True
        if (current.get('execution_records') == state.get('execution_records')
                and current.get('execution_intents', {}) == state.get('execution_intents', {})):
            if current.get('approvals', {}) != state.get('approvals', {}):
                updated = dict(current)
                updated['approvals'] = state.get('approvals', {})
                db.execute('UPDATE conversation_mission_state SET state_json=?,updated_at=? '
                           'WHERE conversation_id=? AND request_key=?',
                           (studio.json.dumps(updated, ensure_ascii=False), studio.now(),
                            conversation_id, request_key))
                return updated, stored_key, True
            return current, stored_key, True
        old_records = current.get('execution_records', {})
        new_records = state.get('execution_records', {})
        if current.get('approvals', {}) != state.get('approvals', {}):
            return current, stored_key, False
        tasks = current.get('plan', {}).get('ordered_tasks', current.get('plan', {}).get('tasks', []))
        valid_ids = {task.get('task_id') for task in tasks if isinstance(task, dict)}
        state_task_ids = {task.get('task_id') for task in
                          state.get('plan', {}).get('ordered_tasks', state.get('plan', {}).get('tasks', []))
                          if isinstance(task, dict)}
        synthetic_state_match = (current.get('synthetic_acceptance') is True
                                 and state.get('synthetic_acceptance') is True
                                 and state.get('_trusted_acceptance_harness') is True
                                 and {str(item).rsplit('.', 1)[-1] for item in state_task_ids}
                                 == {str(item).rsplit('.', 1)[-1] for item in valid_ids})
        if not isinstance(old_records, dict) or not isinstance(new_records, dict):
            return current, stored_key, False
        new_intents = state.get('execution_intents', {})
        if not isinstance(new_intents, dict):
            return current, stored_key, False
        if synthetic_state_match:
            suffix_ids = {str(item).rsplit('.', 1)[-1]: item for item in valid_ids}
            new_records = {suffix_ids.get(str(task_id).rsplit('.', 1)[-1], task_id): record
                           for task_id, record in new_records.items()}
            new_intents = {suffix_ids.get(str(task_id).rsplit('.', 1)[-1], task_id): intent
                           for task_id, intent in new_intents.items()}
            for canonical_id, record in new_records.items():
                record['task_id'] = canonical_id
            state['execution_records'] = new_records
            state['execution_intents'] = new_intents
        if any((task_id not in valid_ids and not (synthetic_state_match and task_id in state_task_ids))
               or not isinstance(record, dict)
               or record.get('task_id') != task_id
               or any(field in record for field in ('authorization_ref', 'outcome', 'task_assessment', 'structural_task_assessment'))
               for task_id, record in new_records.items()):
            return current, stored_key, False
        old_intents = current.get('execution_intents', {})
        if not isinstance(old_intents, dict) or not isinstance(new_intents, dict):
            return current, stored_key, False
        if any(old_intents.get(task_id) != intent
               for task_id, intent in old_intents.items() if task_id not in new_intents):
            return current, stored_key, False
        state_task_ids = {task.get('task_id') for task in
                          state.get('plan', {}).get('ordered_tasks', state.get('plan', {}).get('tasks', []))
                          if isinstance(task, dict)}
        synthetic_state_match = (current.get('synthetic_acceptance') is True
                                 and state.get('synthetic_acceptance') is True
                                 and state.get('_trusted_acceptance_harness') is True
                                 and {str(item).rsplit('.', 1)[-1] for item in state_task_ids}
                                 == {str(item).rsplit('.', 1)[-1] for item in valid_ids})
        if synthetic_state_match:
            suffix_ids = {str(item).rsplit('.', 1)[-1]: item for item in valid_ids}
            new_intents = {suffix_ids.get(str(task_id).rsplit('.', 1)[-1], task_id): intent
                           for task_id, intent in new_intents.items()}
            state['execution_intents'] = new_intents
        if any((task_id not in valid_ids and not (synthetic_state_match and task_id in state_task_ids))
               or not isinstance(intent, dict)
               or set(intent) - {'status', 'request_digest', 'authorization_binding', 'authorization_expiry', 'authorization_id', 'execution_approval'}
               for task_id, intent in new_intents.items()):
            return current, stored_key, False
        for task_id, record in new_records.items():
            old = old_records.get(task_id, {})
            old_status, status = old.get('status'), record.get('status')
            if old_status in {'completed', 'failed', 'cancelled', 'timed_out', 'interrupted', 'denied'}:
                if status != old_status:
                    return current, stored_key, False
            elif old_status == 'authorization_issued':
                if status not in {'authorization_issued', 'running', 'interrupted'}:
                    return current, stored_key, False
                binding_names = ('request_digest', 'authorization_binding', 'authorization_expiry', 'authorization_id', 'execution_approval')
                if any(name in old and record.get(name) != old.get(name) for name in binding_names):
                    return current, stored_key, False
            elif old_status == 'running':
                if status not in {'running', 'completed', 'failed', 'cancelled', 'timed_out', 'interrupted'}:
                    return current, stored_key, False
            elif status not in {'planned', 'blocked', 'authorization_issued', 'denied'}:
                return current, stored_key, False
        # New authorization intent must have its exact separate human decision and
        # binding included in both the task projection and intent ledger.
        for task_id, intent in new_intents.items():
            if task_id not in old_intents:
                record = new_records.get(task_id, {})
                if (record.get('status') != 'authorization_issued'
                        or intent.get('status') != 'authorization_issued'
                        or intent.get('authorization_expiry') != record.get('authorization_expiry')
                        or intent.get('execution_approval') != record.get('execution_approval')
                        or intent.get('authorization_id') != record.get('authorization_id')
                        or intent.get('authorization_binding') != record.get('authorization_binding')
                        or intent.get('request_digest') != record.get('request_digest')
                        or not isinstance(intent.get('execution_approval'), dict)):
                    return current, stored_key, False
            else:
                old_status = old_intents[task_id].get('status')
                status = intent.get('status')
                allowed_intent_transitions = {
                    'authorization_issued': {'authorization_issued', 'running', 'interrupted'},
                    'running': {'running', 'completed', 'failed', 'cancelled', 'timed_out', 'interrupted'},
                    'completed': {'completed'}, 'failed': {'failed'}, 'cancelled': {'cancelled'},
                    'timed_out': {'timed_out'}, 'interrupted': {'interrupted'},
                }
                if (old_status not in allowed_intent_transitions
                        or status not in allowed_intent_transitions[old_status]):
                    return current, stored_key, False
                binding_names = ('authorization_id', 'authorization_binding', 'request_digest',
                                 'authorization_expiry', 'execution_approval')
                if any(intent.get(name) != old_intents[task_id].get(name) for name in binding_names):
                    return current, stored_key, False
        updated = dict(current)
        updated['execution_records'] = new_records
        updated['execution_intents'] = new_intents
        if synthetic_state_match:
            state['execution_records'] = new_records
            state['execution_intents'] = new_intents
            state['plan']['tasks'] = [
                {**task, 'task_id': next((canonical for canonical in valid_ids
                                          if str(canonical).rsplit('.', 1)[-1]
                                          == str(task.get('task_id', '')).rsplit('.', 1)[-1]),
                                         task.get('task_id'))}
                for task in state['plan'].get('tasks', [])]
            state['approvals'] = {
                next((canonical for canonical in valid_ids
                      if str(canonical).rsplit('.', 1)[-1]
                      == str(task_id).rsplit('.', 1)[-1]), task_id): value
                for task_id, value in state.get('approvals', {}).items()}
        updated['execution_performed'] = state.get('execution_performed') is True
        encoded = studio.json.dumps(updated, ensure_ascii=False)
        changed = db.execute('UPDATE conversation_mission_state SET state_json=?,updated_at=? WHERE conversation_id=? AND request_key=? AND state_json=?',
                             (encoded, studio.now(), conversation_id, request_key, row[0])).rowcount
        if changed != 1:
            latest = db.execute('SELECT state_json,request_key FROM conversation_mission_state WHERE conversation_id=?',
                                (conversation_id,)).fetchone()
            return studio.json.loads(latest[0]), latest[1], False
        return updated, stored_key, True


def load_mission_state(conversation_id, *, data_dir=None, db_path=None):
    if not _target(data_dir, db_path).exists():
        return None
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        _ensure_chat_schema(db)
        row = db.execute('SELECT state_json,request_key FROM conversation_mission_state WHERE conversation_id=?',
                         (conversation_id,)).fetchone()
    return (studio.json.loads(row[0]), row[1]) if row else None


def list_sessions(*, data_dir=None, db_path=None, limit=100):
    if not _target(data_dir, db_path).exists():
        return []
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone():
            return []
        return [dict(zip(('conversation_id','title','created_at','updated_at','message_count'), row))
                for row in db.execute('''SELECT c.conversation_id,c.title,c.created_at,c.updated_at,
                    (SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id=c.conversation_id)
                    FROM conversations c ORDER BY c.updated_at DESC LIMIT ?''', (limit,))]


def save_message(conversation_id, role, content, *, message_type='chat', related_lesson_id=None,
                 inference_status='not_applicable', message_id=None, data_dir=None, db_path=None):
    if role not in ('user', 'assistant', 'system') or not isinstance(content, str) or len(content) > 16000:
        raise ValueError('Invalid message.')
    msg_id = message_id or str(uuid.uuid4())
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        _ensure_chat_schema(db)
        if not db.execute('SELECT 1 FROM conversations WHERE conversation_id=?', (conversation_id,)).fetchone():
            raise ValueError('Conversation does not exist.')
        db.execute('INSERT OR IGNORE INTO conversation_messages(message_id,conversation_id,role,created_at,content,message_type,related_lesson_id,inference_status) VALUES(?,?,?,?,?,?,?,?)',
                   (msg_id, conversation_id, role, studio.now(), content, message_type, related_lesson_id, inference_status))
        db.execute('UPDATE conversations SET updated_at=? WHERE conversation_id=?', (studio.now(), conversation_id))
    return msg_id


def load_messages(conversation_id, *, data_dir=None, db_path=None):
    if not _target(data_dir, db_path).exists():
        return []
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'").fetchone():
            return []
        rows = db.execute('SELECT message_id,role,created_at,content,message_type,related_lesson_id,inference_status FROM conversation_messages WHERE conversation_id=? ORDER BY rowid', (conversation_id,)).fetchall()
    keys = ('message_id','role','created_at','content','message_type','related_lesson_id','inference_status')
    return [dict(zip(keys, row)) for row in rows]


def rename_session(conversation_id, title, *, data_dir=None, db_path=None):
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        changed = db.execute('UPDATE conversations SET title=?,updated_at=? WHERE conversation_id=?',
                             (title[:120], studio.now(), conversation_id)).rowcount
        if not changed:
            raise ValueError('Conversation does not exist.')


def delete_session(conversation_id, *, confirmed=False, data_dir=None, db_path=None):
    if confirmed is not True:
        raise ValueError('Explicit confirmation is required to delete conversation history.')
    with studio.connect(initialize=False, data_dir=data_dir, db_path=db_path) as db:
        changed = db.execute('DELETE FROM conversations WHERE conversation_id=?', (conversation_id,)).rowcount
        if not changed:
            raise ValueError('Conversation does not exist.')


def respond_agent_awareness(text, report):
    """Return a validated, read-only answer about declared agent capabilities."""
    from .agent_awareness import answer_agent_question
    try:
        answer = answer_agent_question(text, report)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'I cannot safely answer about agent capabilities because the capability report is invalid.'
    return answer or 'I can share what the verified capability report says if you ask about known agents, their capabilities, or command readiness.'


def respond_general(text, profile):
    folded = text.casefold()
    if re.search(r'\b(how are you|how do you feel)\b', folded):
        return 'I do not have feelings or consciousness. I can help you think through what is happening and distinguish facts from uncertainties.'
    if re.search(r'\b(who are you|hello|hi|hey|good morning|good evening)\b', folded):
        return f"Hello. I am {profile.get('identity_name', 'Sovereign Brain')}. I am here to help you reflect carefully. I do not have personal feelings, but I can discuss emotional situations based on what you tell me."
    return 'I can help with that general-knowledge question.'
