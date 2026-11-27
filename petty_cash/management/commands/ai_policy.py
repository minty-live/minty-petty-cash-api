"""``python manage.py ai_policy`` - the AI readers' settings, each company's grants and use, without SQL.

    python manage.py ai_policy                                   # the policy, and the top companies' use
    python manage.py ai_policy --entity <id>                     # one company: its grants, its last reads
    python manage.py ai_policy --grant <id> --tokens 50000 [--reads 10] [--days 90] [--note "pilot"]
    python manage.py ai_policy --set-default-grant '{"tokens": 280000, "days": 30}'   # the AI trial; 'null': none
    python manage.py ai_policy --set-cycle-tokens 280000                  # each billing period's; 'null': none
    python manage.py ai_policy --set-models gemini-3.8-flash gemini-3.1-pro-preview
    python manage.py ai_policy --set-models gemini-3.8-flash --no-backup
    python manage.py ai_policy --set-price gemini-3.8-flash 0.75 3.75 0.075 --set-models gemini-3.8-flash
    python manage.py ai_policy --set-rules '[{"reads": 20, "period_days": 1}]'
    python manage.py ai_policy --receipt-reader off --invoice-reader on      # the readers' switches
    python manage.py ai_policy --timeout-s 30 --max-output-tokens 6000 --thinking-level medium --max-file-mb 10

minty-db's ``ai_policy`` row and ``ai_grant`` rows (owner, 2026-11-18) are shared by Petty Cash and
Payment Request, and both services read them on every read: a change here takes effect on the next
read, no restart. A company reads against its grants: ``default_grant`` is its 30-day AI trial
(once, from its first module subscription), ``cycle_tokens`` one grant per billing period after it
(the user, 2026-11-27; prorated like billing), and ``--grant`` gives it another by hand (a top-up). The readers' switches and their timeout, token budget, thinking level and largest document
are in the same row (owner, 2026-11-20; they were each service's environment). There is no region:
every read is on Vertex AI's global endpoint (owner, 2026-11-23). Each model's price (USD per 1M
tokens - input, output, cached input; the user, 2026-11-24) is ``model_prices``: a model is named
only with its price, so give ``--set-price`` with ``--set-models`` for a new one. A change is checked
first (``minty_shared.django.ai_allowance``) and refused with the reason, so a typo cannot switch
reading off. An UPDATE in SQL does the same, unchecked.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q, Sum
from minty_db.models import AiPolicy, AiRead, Entity
from minty_shared import ai
from minty_shared.django import ai_allowance


class Command(BaseCommand):
    help = ("Show or change the AI readers' models, switches, limits, default grant and caps (ai_policy), "
            "and give grants.")

    def add_arguments(self, parser):
        parser.add_argument("--set-models", nargs="+", metavar="MODEL",
                            help="the primary model, then optionally the backup")
        parser.add_argument("--no-backup", action="store_true", help="with --set-models: no backup model")
        parser.add_argument("--set-price", nargs=4, action="append", metavar=("MODEL", "INPUT", "OUTPUT", "CACHED"),
                            help="a model's Vertex AI price in USD per 1M tokens: input, output, cached input")
        parser.add_argument("--set-default-grant", metavar="JSON",
                            help='the AI trial every company gets, e.g. \'{"tokens": 280000, "days": 30}\', or null')
        parser.add_argument("--set-cycle-tokens", metavar="N",
                            help="the tokens a company gets each billing period after its trial, or null")
        parser.add_argument("--set-rules", metavar="JSON",
                            help='extra caps over every company, e.g. \'[{"reads": 20, "period_days": 1}]\'')
        parser.add_argument("--receipt-reader", choices=("on", "off"), help="Petty Cash's receipt reader")
        parser.add_argument("--invoice-reader", choices=("on", "off"), help="Payment Request's invoice reader")
        parser.add_argument("--timeout-s", type=int, help="each model call's limit in seconds (5-120)")
        parser.add_argument("--max-output-tokens", type=int, help="the reply's budget, thinking included (256-65536)")
        parser.add_argument("--thinking-level", help="minimal, low, medium or high")
        parser.add_argument("--max-file-mb", type=int, help="the largest document read (1-20 MB)")
        parser.add_argument("--grant", metavar="ENTITY_ID", help="give this company a grant (with --tokens/--reads)")
        parser.add_argument("--tokens", type=int, help="with --grant: the tokens it gives")
        parser.add_argument("--reads", type=int, help="with --grant: the reads it gives")
        parser.add_argument("--days", type=int, help="with --grant: it expires after this many days")
        parser.add_argument("--note", help="with --grant: why it was given")
        parser.add_argument("--entity", metavar="ID", help="one company's allowance, grants and last reads")
        parser.add_argument("--by", default="command line", help="who is changing it (updated_by / granted_by)")

    def handle(self, *args, **options):
        row = AiPolicy.objects.filter(id=1).first()
        changing = (options["set_models"] or options["set_rules"] or options["set_default_grant"] is not None
                    or options["set_cycle_tokens"] is not None or options["set_price"]
                    or bool(_settings_given(options)))
        if changing:
            if row is None:
                raise CommandError("There is no ai_policy row. Load seed_catalogue.sql (or minty-db's "
                                   "2026-11-18_ai_policy_and_reads.sql) first.")
            self._change(row, options)
        if options["grant"]:
            self._grant(options)
        self._show(options.get("entity") or options.get("grant"))

    def _change(self, row: AiPolicy, options) -> None:
        primary, backup = row.primary_model, row.backup_model
        rules, default_grant, cycle_tokens = row.limit_rules, row.default_grant, row.cycle_tokens
        if options["set_models"]:
            if len(options["set_models"]) > 2:
                raise CommandError("Give at most two models: the primary, then the backup.")
            primary = options["set_models"][0]
            if len(options["set_models"]) == 2:
                backup = options["set_models"][1]
            elif options["no_backup"]:
                backup = None
        if options["set_rules"]:
            rules = _json(options["set_rules"], "--set-rules")
        if options["set_default_grant"] is not None:
            default_grant = _json(options["set_default_grant"], "--set-default-grant")
        if options["set_cycle_tokens"] is not None:
            cycle_tokens = _json(options["set_cycle_tokens"], "--set-cycle-tokens")
        settings = {key: getattr(row, key) for key in ai_allowance.SETTING_FIELDS} | _settings_given(options)
        prices = dict(row.model_prices or {})
        for model, *numbers in options["set_price"] or []:
            try:
                prices[model] = dict(zip(("input", "output", "cached_input"), map(float, numbers), strict=True))
            except ValueError:
                raise CommandError(f"Not changed: the price of {model} must be three numbers.") from None
        for model in (primary, backup):
            if model and ai.provider_for(model.strip())[0] is None:
                raise CommandError(f"Not changed: {model} is not a model of an approved provider "
                                   f"({', '.join(sorted(ai.APPROVED_PROVIDERS))}).")
        try:
            policy = ai_allowance.parse_policy(primary, backup, rules, default_grant, settings, prices, cycle_tokens)
        except ai_allowance.PolicyError as exc:
            raise CommandError(f"Not changed: {exc}") from None
        if backup and backup == primary:
            raise CommandError("Not changed: the backup must be a different model from the primary.")
        AiPolicy.objects.filter(id=1).update(primary_model=policy.primary_model, backup_model=policy.backup_model,
                                             limit_rules=rules, default_grant=default_grant,
                                             cycle_tokens=policy.cycle_tokens, model_prices=prices,
                                             updated_by=options["by"], **settings)
        self.stdout.write(self.style.SUCCESS("ai_policy changed - it applies from the next read."))

    def _grant(self, options) -> None:
        entity = Entity.objects.filter(id=options["grant"]).first()
        if entity is None:
            raise CommandError(f"No company {options['grant']}.")
        raw = {k: options[k] for k in ("tokens", "reads", "days") if options[k] is not None}
        try:
            spec = ai_allowance.parse_grant(raw, "--grant")
        except ai_allowance.PolicyError as exc:
            raise CommandError(f"Not given: {exc}") from None
        assert spec is not None
        ai_allowance.give_grant(entity.id, spec, granted_by=options["by"], note=options["note"])
        self.stdout.write(self.style.SUCCESS(f"Grant given to {entity.name} - it can read from now."))

    def _show(self, entity_id: str | None) -> None:
        policy = ai_allowance.load_policy()
        if policy is None:
            self.stdout.write(self.style.ERROR("ai_policy: missing or unusable - AI reading is OFF."))
            return
        self.stdout.write(f"models        : {' -> '.join(policy.models)}")
        s = policy.settings
        self.stdout.write(f"readers       : receipt {_on(s.receipt_reader_on)}, invoice {_on(s.invoice_reader_on)}")
        self.stdout.write(f"limits        : timeout {s.timeout_s} s, {s.max_output_tokens} output tokens, "
                          f"thinking {s.thinking_level}, files up to {s.max_file_mb} MB")
        self.stdout.write(f"region        : {ai.VERTEX_LOCATION} (Vertex AI)")
        for model, price in policy.prices:
            self.stdout.write(f"price         : {model} USD {price.input} in / {price.output} out / "
                              f"{price.cached_input} cached in, per 1M tokens")
        self.stdout.write(f"trial grant   : {json.dumps(_spec(policy.default_grant))}")
        self.stdout.write(f"period grant  : {json.dumps(policy.cycle_tokens)} tokens")
        self.stdout.write(f"extra caps    : {json.dumps([_rule(r) for r in policy.rules])}")
        if entity_id:
            allowance = ai_allowance.status(entity_id, policy)
            self.stdout.write(f"entity {entity_id}: {json.dumps(allowance.as_dict())}")
            for g in ai_allowance.grants_of(entity_id):
                caps = ", ".join(
                    f"{used}/{total} {what}"
                    for what, used, total in (("tokens", g.tokens_used, g.tokens_total),
                                              ("reads", g.reads_used, g.reads_total))
                    if total is not None
                )
                expiry = f" expires {g.expires_at:%Y-%m-%d}" if g.expires_at else ""
                self.stdout.write(f"  grant {g.id} ({g.source}): {caps}{expiry}")
            for read in AiRead.objects.filter(entity_id=entity_id).order_by("-created_at")[:20]:
                self.stdout.write(f"  {read.created_at:%Y-%m-%d %H:%M} {read.module:<15} {read.status:<7} "
                                  f"{read.attempts or '-'} tokens={read.tokens_in + read.tokens_out + read.tokens_thought}")
            return
        top = (AiRead.objects.values("entity_id")
               .annotate(reads=Count("id", filter=Q(status="ok")), failed=Count("id", filter=Q(status="failed")),
                         tokens=Sum("tokens_in") + Sum("tokens_out") + Sum("tokens_thought"))
               .order_by("-reads")[:20])
        names = dict(Entity.objects.filter(id__in=[t["entity_id"] for t in top]).values_list("id", "name"))
        self.stdout.write("companies by reads (all time): reads / failed / tokens")
        for t in top:
            self.stdout.write(f"  {names.get(t['entity_id'], t['entity_id'])}: {t['reads']} / {t['failed']} / "
                              f"{t['tokens'] or 0}")


def _settings_given(options) -> dict:
    """The reader settings this run changes, by their ``ai_policy`` column."""
    given = {}
    for flag, column in (("receipt_reader", "receipt_reader_on"), ("invoice_reader", "invoice_reader_on")):
        if options.get(flag) is not None:
            given[column] = options[flag] == "on"
    for column in ("timeout_s", "max_output_tokens", "thinking_level", "max_file_mb"):
        if options.get(column) is not None:
            given[column] = options[column]
    return given


def _on(value: bool) -> str:
    return "on" if value else "off"


def _json(text: str, flag: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError(f"{flag} is not JSON: {exc}") from None


def _rule(rule: ai_allowance.Rule) -> dict:
    return {k: v for k, v in (("reads", rule.reads), ("tokens", rule.tokens), ("period_days", rule.period_days))
            if v is not None}


def _spec(spec: ai_allowance.GrantSpec | None) -> dict | None:
    if spec is None:
        return None
    return {k: v for k, v in (("tokens", spec.tokens), ("reads", spec.reads), ("days", spec.days)) if v is not None}
