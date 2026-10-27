-- Arena Pro: separate registration and payment deadlines.
-- Apply in a staging branch, validate, and only then migrate production.
-- Existing events remain on their original behavior until payment_deadline is configured.
ALTER TABLE public.sport_events
  ADD COLUMN IF NOT EXISTS payment_deadline timestamptz;
COMMENT ON COLUMN public.sport_events.payment_deadline
  IS 'Final deadline for external/manual and gateway payment confirmation. Null means legacy behavior.';
CREATE INDEX IF NOT EXISTS idx_sport_events_payment_deadline
  ON public.sport_events(payment_deadline)
  WHERE payment_deadline IS NOT NULL;
-- Deliberately no bulk backfill: existing events must be reviewed before changing their rules.
