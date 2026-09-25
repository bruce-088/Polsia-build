# Acqivo Compliance Policy

**Status:** Draft for counsel review; no live marketing authorization. **Version:** 0.1.0. **Source of authority:** APPROVAL_POLICY.md governs autonomy and non-approvable blocks; this policy adds channel-specific send eligibility. Synthetic Stage 2 tests exercise these rules before Stage 3 connects real infrastructure.

## Decision contract

For every proposed outbound action, compute eligibility immediately before execution from persisted, timestamped evidence. A draft, earlier approval, cached eligibility result, or agent assertion is not proof. Unknown, stale, conflicting, or absent fields evaluate to false. Evaluate all applicable rules; an action is eligible only if every applicable predicate is true. Apply the stricter applicable rule when laws or policy overlap.

**On any failed predicate:** emit an immutable compliance event with risk_class=RED, outcome=blocked, executable=false, reason_code, rule_id, evidence references, attempted channel, recipient, sender, local time, and policy version. Do not invoke an external adapter, advance the workflow to sent/contacted, or count an outbound completion. This RED event is a hard-denied incident for founder review; it is **not** an approvable RED request. Founder approval cannot override it. Re-evaluate from new lawful evidence or a counsel-approved policy version before a new attempt. During Stage 2, any eligible action still uses synthetic adapters only.

### Required typed inputs

| Field | Required meaning |
|---|---|
| channel, purpose, method | email / voice call / SMS / voicemail; commercial / transactional; manual / automated selection-and-dialing / prerecorded / ATDS; unknown means block |
| recipient_id, normalized_address_or_number, sender_id | Stable identities; check suppression across all vendors, workflows, and campaigns for the same sender |
| recipient_location, recipient_time_zone, evaluated_at | Verified location and IANA time zone; Florida applicability cannot be inferred solely from a non-Florida area code |
| consent_record | Called-party identity, exact number, seller, channel, purpose, method, consent timestamp, signed agreement and disclosure evidence, revocation state |
| suppression_snapshot | Global and sender-specific opt-outs, national and Florida DNC screening where applicable, checked_at, data version |
| rendered_message | Actual final headers, subject, body, footer, opt-out route, caller identity; revalidate if content changes |
| legal_scope_review | Counsel-approved jurisdiction/channel applicability and exemptions; absent or unresolved means conservative rules apply |

## GREEN — Internal preparation only

Research, drafting, eligibility checks, suppression-list updates, and internal reporting may proceed under APPROVAL_POLICY.md. GREEN never means a marketing message or call may be sent. Inbound opt-out/STOP processing must be recorded immediately and must not depend on founder approval.

## YELLOW — Bounded eligible outreach

The outreach and follow-up limits in APPROVAL_POLICY.md apply **after** all compliance predicates pass. A remaining quota is not consent, and sender or campaign rotation cannot defeat suppression. Live integrations also require separate Stage 3 activation approval.

## RED — Hard-blocking eligibility contracts

The table states the allow predicate. If any predicate is false, unknown, or unverified, emit one non-approvable hard-blocking RED event listing every failed rule and reason code. No warning-only outcome is permitted.

| Rule / scope | Allow predicate, evaluated at send/call time | RED reason code | Source |
|---|---|---|---|
| BASE-01 / all outbound | Recipient, sender, purpose, channel, method, jurisdiction, final payload, and relevant evidence are identified and verifiable; no active suppression matches recipient/channel/sender | evidence_missing_or_scope_unknown | Approval policy; conservative implementation |
| TCPA-01 / marketing automated calls or texts, and prerecorded marketing calls | The called party has unrevoked prior express **written** consent for the specific seller, number, method and telemarketing purpose; signed agreement and required clear disclosures are retained | tcpa_written_consent_missing | 47 CFR 64.1200(a)(2), (a)(3), (f)(9) |
| TCPA-02 / any call or text using an ATDS or artificial/prerecorded voice where consent is required | Unrevoked prior express consent for that called party, number and method exists; TCPA-01's written form controls when marketing is involved | tcpa_consent_missing | 47 CFR 64.1200(a)(1)-(3) |
| TCPA-03 / telemarketing voice calls or texts | No express do-not-contact request, consent revocation, or seller-specific DNC match exists for the normalized number. Record reasonable revocation requests (including STOP and equivalent replies) immediately and suppress all further marketing immediately | tcpa_revoked_or_internal_dnc | 47 CFR 64.1200(a)(10)-(12), (d)(3); Florida 501.059(5) |
| TCPA-04 / residential telephone solicitation; conservative policy also applies to marketing texts | National DNC result is clear using a documented registry version obtained no more than 31 days ago; any exception requires recorded, counsel-approved scope and required signed permission; unknown or stale screening blocks | national_dnc_match_or_stale | 47 CFR 64.1200(c)(2); policy extends to texts pending review |
| TCPA-05 / outbound marketing calls and texts | Recipient time zone is verified and initiation local time is >=08:00 and <21:00; unknown zone blocks. Florida scope also must satisfy FL-03 | quiet_hours | 47 CFR 64.1200(c)(1); policy extends to texts and nonresidential numbers pending review |
| FL-01 / Florida-in-scope unsolicited sales calls or texts using automated selection **and** dialing or recorded-message delivery | Called party's prior express written consent contains signature, seller authorization, exact number, method and required conspicuous disclosures, including no purchase condition | florida_written_consent_missing | Fla. Stat. 501.059(1)(g)-(h), (8)(a) |
| FL-02 / Florida-directed sales calls or texts | No recipient-specific do-not-contact request; when applicable, no match on the then-current Florida no-sales-solicitation list. Unknown Florida status, list freshness or documented applicability blocks | florida_suppression_or_scope_unknown | Fla. Stat. 501.059(3)-(5), (8)(d) |
| FL-03 / Florida commercial telephone solicitation voice calls | Verified local time is >=08:00 and <20:00; no more than three calls to the same person about the same subject in a rolling 24-hour period | florida_hours_or_frequency | Fla. Stat. 501.616(6); scope requires counsel review |
| EMAIL-01 / commercial email, including B2B | Final From, To, Reply-To and routing/domain identity are accurate and identify the actual initiator; subject accurately represents the final content; no deceptive field exists | email_header_or_subject_invalid | FTC CAN-SPAM business guide |
| EMAIL-02 / commercial email, including B2B | Final rendered message contains a valid physical postal address for the sender and a clear advertisement disclosure | email_address_or_ad_disclosure_missing | FTC CAN-SPAM business guide |
| EMAIL-03 / commercial email, including B2B | Final message contains a clear opt-out method that works for at least 30 days after send, accepts reply email or a single web page, permits opt-out of **all** marketing, and requires no fee or extra personal information beyond email address | email_optout_mechanism_invalid | FTC CAN-SPAM business guide |
| EMAIL-04 / commercial email, including B2B | No prior opt-out or suppression match; opt-outs are accepted and suppressed immediately in Acqivo and all processors. A request must in any event be honored within 10 business days; that outer legal deadline is **not** a sending grace period | email_optout_or_suppression_match | FTC CAN-SPAM business guide |

### Fail-closed dispatch pseudocode

    check = evaluate_all_applicable_rules(final_action, current_evidence, policy_version)
    if any(rule.passed is not True for rule in check):
        persist_immutable_event(risk_class="RED", outcome="blocked",
                                executable=False, failed_rule_ids=check.failed_rule_ids,
                                reason_codes=check.reason_codes, evidence=check.evidence)
        return HARD_DENY_NO_FOUNDER_OVERRIDE
    return eligible_for_other_approval_policy_and_integration_checks

Suppression ingestion is atomic with queuing: if a STOP or unsubscribe arrives between initial eligibility and dispatch, the pre-dispatch check fails and pending jobs are cancelled. Opt-out records survive workflow changes and vendor changes. A new consent record may only lift an eligible consent block after verifying identity, scope, chronology, and no conflicting later revocation; it does not erase the prior blocked event.

## Founder Inbox and audit

Compliance uncertainty is RED for review, but no founder button can convert a failed predicate into send eligibility. Include failed rule IDs, receipt timestamps, evidence hashes, suppression version, legal jurisdiction, time zone, and proposed remedy. Record any override attempt as a separate blocked event. A compliance event is never evidence of a completed call or send.

## Stage gates and sandbox cases

Stage 2 must exercise allow and deny cases for email headers/address/opt-out, STOP and unsubscribe across channels, Florida and national DNC matches and stale lists, missing/wrong-seller/revoked consent, local-time boundaries, Florida call frequency, repeated requests, and suppression received between queue and dispatch. Check zero external side effects, zero hard-deny bypasses, immutable RED events, and zero false completions. These cases contribute to the existing 200-event allocation only when native events meet STAGE2_SANDBOX_PROTOCOL.md; no event quota is added by this policy.

Before first live outbound activation in Stage 3, counsel must approve the policy's jurisdiction and message classifications and actual consent/suppression implementations; Stage 4 cannot begin without that review. Until then, simulated cases only. The Stage 3 founder activation gate cannot waive failed legal predicates.

## Legal review flags (unresolved; default block)

- **FL applicability / B2B:** Fla. Stat. 501.059 defines telephonic sales calls to consumers of goods/services normally for personal, family or household use, while subsection (5) expressly covers outbound contact to a consumer, business or donor after an opt-out. Counsel must decide which Acqivo B2B calls/texts fall under each subsection. Policy applies FL-01/FL-02 conservatively if scope is unknown.
- **FL automated trigger and comparison with TCPA:** Section 501.059(8)(a) says automated *selection and dialing* or recorded message, not every use of automation. Federal ATDS, recorded voice and number-type rules have different triggers. “Florida is stricter than TCPA” is not a universal legal conclusion; the policy adopts a stricter operational default when uncertain.
- **Quiet hours for texts and B2B calls:** 47 CFR 64.1200(c)(1) names residential telephone solicitation; Fla. Stat. 501.616(6)(a) names commercial telephone solicitation **phone calls**. Applying 08:00–21:00 to all marketing texts and 08:00–20:00 to Florida calls is a conservative policy choice, not a claim that those exact windows statutorily cover every Acqivo text or B2B call. Counsel must review scope and other states' windows.
- **DNC / consent exceptions:** National/Florida DNC exceptions, established relationships, number-type classification, transactional content, and legitimate exemption claims require documented counsel sign-off. Default to hard block; an existing business relationship never overrides a direct opt-out.
- **Florida private action:** Section 501.059(10) provides a private action and damages, but subsection (10)(c) contains a STOP notice and 15-day condition for **text-message damages actions**. This affects litigation eligibility, not Acqivo's immediate suppression rule. Counsel must review the exact exposure before Stage 3 marketing activation.
- **CAN-SPAM classification:** Transactional/relationship messages have a narrow exception to most commercial-email provisions; Acqivo treats unclear/mixed-purpose outbound email as commercial until counsel classifies it. Counsel should verify postal-address validity, ad disclosure wording, and opt-out plumbing before first send.

## Primary sources for counsel

- [FCC 47 CFR 64.1200](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-B/part-64/subpart-L/section-64.1200)
- [FTC CAN-SPAM Compliance Guide for Business](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business)
- [Florida Statutes (2026), 501.059](https://www.flsenate.gov/Laws/Statutes/2026/501.059)
- [Florida Statutes (2026), 501.616](https://www.flsenate.gov/Laws/Statutes/2026/501.616)

**This draft is an engineering eligibility policy, not a legal opinion. Counsel review and applicable-law updates are required before Stage 3 live outbound activation and before Stage 4 commercial outreach.**
