-- A pipeline statement sqlglot can't parse. The explorer must report it as a
-- lineage gap on raw_crm.contacts, not drop it: it is the gaps report's
-- known-bad control.
APPLY CHANGES INTO raw_crm.contacts
FROM STREAM(raw_crm.contacts_feed)
KEYS (contact_id)
SEQUENCE BY updated_at;
