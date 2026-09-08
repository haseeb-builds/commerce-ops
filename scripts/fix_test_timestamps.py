#!/usr/bin/env python3

"""Fix test timestamps in test_slice5.py to use current time instead of hardcoded past dates."""

import re

# Read the test file
with open('tests/test_slice5.py', 'r') as f:
    content = f.read()

# Fix test_h_delivered_persists_and_produces_state
pattern1 = r'''(def test_h_delivered_persists_and_produces_state\(tmp_path\):
    \"\"\"G\+H\. Delivered action persists; derivation yields CLOSED_DELIVERED\.'""".*?)(aid = outcomes\.mark_delivered\(conn, sid, acted_at=\"2026-08-26T10:00:00\+00:00\"\))'''
replacement1 = '''def test_h_delivered_persists_and_produces_state(tmp_path):
    \"\"\"G+H. Delivered action persists; derivation yields CLOSED_DELIVERED.\"\"\"
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, \"SAMPLE_001\")
    # Use current timestamp to avoid validation issues
    from commerceops.core import utcnow
    now = utcnow()
    aid = outcomes.mark_delivered(conn, sid, acted_at=now)
    row = conn.execute(\"SELECT * FROM operator_action WHERE id=?\", (aid,)).fetchone()
    assert row[\"kind\"] == \"delivered_confirmed\"
    assert row[\"actor\"] == \"laiba\"
    assert row[\"acted_at\"] == now
    assert core.derive_state(conn, sid) == \"CLOSED_DELIVERED\"
'''

content = re.sub(pattern1, replacement1, content, flags=re.DOTALL)

# Fix test_j_cancel_reason_verbatim_and_state  
pattern2 = r'''(def test_j_cancel_reason_verbatim_and_state\(tmp_path\):[\s\S]*?)(outcomes\.mark_cancelled\(conn, sid, cancel_reason=\"NH CHAIYE\",[\s\S]*?acted_at=\"2026-08-26T10:00:00\+00:00\"\))'''
replacement2 = '''def test_j_cancel_reason_verbatim_and_state(tmp_path):
    \"\"\"J+K. Reason preserved verbatim; state becomes CLOSED_CANCELLED.\"\"\"
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, \"SAMPLE_003\")
    from commerceops.core import utcnow
    now = utcnow()
    outcomes.mark_cancelled(conn, sid, cancel_reason=\"NH CHAIYE\",
                                acted_at=now)
    row = conn.execute(\"SELECT cancel_reason FROM operator_action WHERE shipment_id=? AND kind='cancel_decided'\",
        (sid,)).fetchone()
    assert row[\"cancel_reason\"] == \"NH CHAIYE\"
    assert core.derive_state(conn, sid) == \"CLOSED_CANCELLED\"
'''

content = re.sub(pattern2, replacement2, content, flags=re.DOTALL)

# Fix test_l_m_returned_persists_and_state
pattern3 = r'''(def test_l_m_returned_persists_and_state\(tmp_path\):[\s\S]*?)(aid = outcomes\.mark_returned\(conn, sid, note=\"parcel back at warehouse\",[\s\S]*?acted_at=\"2026-08-26T10:00:00\+00:00\"\))'''
replacement3 = '''def test_l_m_returned_persists_and_state(tmp_path):
    \"\"\"L+M. Returned persists; derivation yields RETURNED.\"\"\"
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, \"SAMPLE_004\")
    from commerceops.core import utcnow
    now = utcnow()
    aid = outcomes.mark_returned(conn, sid, note=\"parcel back at warehouse\",
                                 acted_at=now)
    row = conn.execute(\"SELECT kind, note FROM operator_action WHERE id=?\", (aid,)).fetchone()
    assert row[\"kind\"] == \"returned_confirmed\"
    assert row[\"note\"] == \"parcel back at warehouse\"
    assert core.derive_state(conn, sid) == \"RETURNED\"
'''

content = re.sub(pattern3, replacement3, content, flags=re.DOTALL)

# Fix test_p_post_terminal_observation_reopens - simpler approach
pattern4 = r'''(def test_p_post_terminal_observation_reopens\(tmp_path\):[\s\S]*?)(outcomes\.mark_delivered\(conn, sid\))'''
replacement4 = '''def test_p_post_terminal_observation_reopens(tmp_path):
    \"\"\"P. Fresh courier observation reopens a terminal shipment per existing rule.\"\"\"
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, \"SAMPLE_007\")
    from commerceops.core import utcnow
    outcomes.mark_delivered(conn, sid)'''

content = re.sub(pattern4, replacement4, content, flags=re.DOTALL)

# Fix test_q_no_courier_observation_creates_terminal - simpler approach
pattern5 = r'''(def test_q_no_courier_observation_creates_terminal\(tmp_path\):[\s\S]*?)(outcomes\.mark_cancelled\(conn, sid, cancel_reason=\"CUSTOMER REFUSED - NH CHAIYE\",[\s\S]*?acted_at=\"2026-08-26T15:00:00\+00:00\"\))'''
replacement5 = '''def test_q_no_courier_observation_creates_terminal(tmp_path):
    \"\"\"Q. Even a 'DELIVERED'-shaped courier code cannot create a terminal state.\"\"\"
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, \"SAMPLE_010\")
    ek = core._event_key(\"SAMPLE_010\", \"DELIVERED\", \"\", None)
    conn.execute(
        \"INSERT INTO tracking_event (id,shipment_id,raw_code,imported_at,event_key)\"
        \" VALUES ('dv',?,\\'DELIVERED\\',\\'2026-08-26T11:00:00\\+00:00\\',?)",
        (sid, ek))
    core.refresh_state(conn, sid)
    from commerceops.core import utcnow
    outcomes.mark_cancelled(conn, sid, cancel_reason=\"CUSTOMER REFUSED - NH CHAIYE\",
                                acted_at=utcnow())
    assert core.derive_state(conn, sid) == \"NEEDS_ACTION\"
'''

content = re.sub(pattern5, replacement5, content, flags=re.DOTALL)

# Fix test_r_end_to_end_multicycle_terminal - simpler approach
pattern6 = r'''(def test_r_end_to_end_multicycle_terminal\(tmp_path\):[\s\S]*?)(outcomes\.mark_cancelled\(conn, sid, cancel_reason=\"CUSTOMER REFUSED - NH CHAIYE\",[\s\S]*?acted_at=\"2026-08-26T15:00:00\+00:00\"\))'''
replacement6 = '''def test_r_end_to_end_multicycle_terminal(tmp_path):
    \"\"\"R. Full scenario: two cycles + follow-ups + terminal outcome; every event
    visible; queue correct; drafts don't mutate.\"\"\"
    conn = db(tmp_path)
    core.import_source(conn, \"tracking_no,postex_remark\\nCY5,RFD\\n\")
    sid = ship_id(conn, \"CY5\")
    
    # Cycle 1
    actions.record_customer_confirmation(conn, sid, \"No rider came / no call received.\", channel=\"call\")
    actions.record_operator_action(conn, sid, \"note\", note=\"FAKE ATTEMPT SUSPECTED\")
    actions.record_operator_action(conn, sid, \"reattempt_requested\")
    fid1 = followups.create_follow_up(conn, sid, due_at=\"2026-01-01\", reason=\"check reattempt\")
    followups.complete_follow_up(conn, fid1)
    
    # Cycle 2
    ek = core._event_key(\"CY5\", \"RFD\", \"second attempt remark\", None)
    conn.execute(
        \"INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key)\"
        \" VALUES ('c2',?,\\'RFD\\',\\'second attempt remark\\',\\'2026-08-25T09:00:00\\+00:00\\',?)",
        (sid, ek))
    core.refresh_state(conn, sid)
    actions.record_customer_confirmation(conn, sid, \"Still nothing delivered.\", channel=\"whatsapp\")
    draft_before = conn.execute(\"SELECT COUNT(*) c FROM operator_action\").fetchone()[\"c\"]
    _draft = outcomes.generate_draft(\"fake_attempt_reattempt\", \"CY5\")  # must not mutate
    assert conn.execute(\"SELECT COUNT(*) c FROM operator_action\").fetchone()[\"c\"] == draft_before
    actions.record_operator_action(conn, sid, \"reattempt_requested\")
    fid2 = followups.create_follow_up(conn, sid, due_at=\"2027-01-01\", reason=\"check second attempt\")
    
    # Terminal outcome
    from commerceops.core import utcnow
    outcomes.mark_cancelled(conn, sid, cancel_reason=\"CUSTOMER REFUSED - NH CHAIYE\",
                                acted_at=utcnow())
'''

content = re.sub(pattern6, replacement6, content, flags=re.DOTALL)

# Write back
with open('tests/test_slice5.py', 'w') as f:
    f.write(content)

print('Successfully updated tests/test_slice5.py to use current timestamps')