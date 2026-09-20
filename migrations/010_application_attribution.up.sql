-- Record where a subscription's owner and department came from, so a sync can fill a blank
-- without ever overwriting a person's decision.
--
-- `gateway_application.owner_id` and `.department_id` have existed since 001 and nothing has
-- ever written them: the adoption path inserts both as literal NULL. That was survivable while
-- attribution came from request headers. It is not survivable here, because at the install this
-- was built for nothing sends those headers -- every request on the gateway for a year attributed
-- to `unattributed`, which the budget rollup drops on the floor. Department and person budgets
-- therefore counted zero, forever, no matter what anyone configured.
--
-- The fix is to attribute from the subscription itself, which the gateway already identifies on
-- every request. That makes these two columns load-bearing, and the moment they are load-bearing
-- the question becomes: who is allowed to write them, and what happens on the next sync?
--
-- Three sources, in descending authority:
--
--   apim     -- the APIM subscription's own ownerId, resolved to that APIM user's email. This is
--               not a guess: the customer's own directory says who holds the key.
--   derived  -- the person read out of the subscription's display name, after the usage suffix
--               is stripped ("a-zagainov-IT" and "a-zagainov-IT - Databricks" are one person
--               holding two keys). A guess, and labelled as one on screen.
--   manual   -- someone looked at it and said so.
--
-- A sync may write `apim` and `derived` over a blank or over each other. It may never write over
-- `manual`. Without that rule the honest options are both bad: either sync overwrites corrections
-- every month, or it never fills anything and an install with three hundred subscriptions stays
-- blank forever. This table exists to make the third option possible.
--
-- Why a side table rather than two more columns on `gateway_application`: the registry reads that
-- row with `SELECT *` into a model that forbids unknown fields, so a new column there means the
-- previous release cannot read the table it is rolled back onto. A side table is invisible to
-- code that does not know about it, which is exactly what a rollback needs.

CREATE TABLE public.gateway_application_attribution (
    application_id uuid PRIMARY KEY
        REFERENCES public.gateway_application(id) ON DELETE CASCADE,
    owner_source text CHECK (
        owner_source IS NULL
        OR owner_source = ANY (ARRAY['apim'::text, 'derived'::text, 'manual'::text])
    ),
    department_source text CHECK (
        department_source IS NULL
        OR department_source = ANY (ARRAY['apim'::text, 'derived'::text, 'manual'::text])
    ),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.gateway_application_attribution IS
    'Provenance for gateway_application.owner_id and .department_id. A sync fills apim/derived; '
    'only a person writes manual, and a sync never overwrites it.';

-- The work list on the subscriptions screen is "everything still unattributed", and on an install
-- with several hundred keys that query runs on every page load.
CREATE INDEX gateway_application_attribution_owner_source_idx
    ON public.gateway_application_attribution (owner_source)
    WHERE owner_source IS NOT NULL;

CREATE TABLE public.gateway_application_attribution_audit (
    id uuid PRIMARY KEY,
    application_id uuid NOT NULL
        REFERENCES public.gateway_application(id) ON DELETE CASCADE,
    field text NOT NULL CHECK (field = ANY (ARRAY['owner'::text, 'department'::text])),
    previous_value text,
    new_value text,
    previous_source text,
    new_source text NOT NULL,
    changed_by text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX gateway_application_attribution_audit_application_idx
    ON public.gateway_application_attribution_audit (application_id, changed_at DESC);
