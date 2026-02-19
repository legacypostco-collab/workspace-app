from dataclasses import dataclass


@dataclass(frozen=True)
class AutoModeInputs:
    part_found: bool
    confidence: float
    trusted_count: int
    sandbox_count: int
    fresh_data: bool = True


@dataclass(frozen=True)
class AutoModeDecision:
    eligible_auto: bool
    next_state: str
    reason: str


def decide_auto_mode(inputs: AutoModeInputs, confidence_threshold: float = 70.0) -> AutoModeDecision:
    issues: list[str] = []
    if not inputs.part_found:
        issues.append("part_not_found")
    if not inputs.fresh_data:
        issues.append("stale_data")
    if inputs.confidence < confidence_threshold:
        issues.append("confidence_below_threshold")
    if inputs.trusted_count < 1:
        issues.append("no_trusted_supplier")
    if (inputs.trusted_count + inputs.sandbox_count) < 3:
        issues.append("offers_less_than_3")

    if issues:
        return AutoModeDecision(
            eligible_auto=False,
            next_state="needs_review",
            reason="auto_disabled:" + ",".join(issues),
        )

    return AutoModeDecision(
        eligible_auto=True,
        next_state="auto_matched",
        reason="auto_enabled:all_conditions_met",
    )


def can_be_executor(
    mode: str,
    supplier_status: str,
    has_trusted_for_position: bool,
    operator_confirmed: bool = False,
    risky_double_confirmed: bool = False,
) -> bool:
    # REJECTED is always blocked in all modes.
    if supplier_status == "rejected":
        return False

    if mode == "auto":
        if supplier_status == "trusted":
            return True
        # SANDBOX cannot be the automatic sole executor when trusted exists.
        if supplier_status == "sandbox":
            return (not has_trusted_for_position) and operator_confirmed
        return False

    # SEMI / MANUAL OEM require operator actions for non-trusted statuses.
    if mode in {"semi", "manual_oem"}:
        if supplier_status == "trusted":
            return True
        if supplier_status == "sandbox":
            return operator_confirmed
        if supplier_status == "risky":
            return operator_confirmed and risky_double_confirmed
        return False

    return False
