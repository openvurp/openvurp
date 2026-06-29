"""
openvurp Core — Task Planner

Task decomposition, step tracking, adaptive replanning.
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


class StepStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Step:
    description: str
    status: StepStatus = StepStatus.PENDING
    tools_needed: list[str] = field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def start(self):
        self.status = StepStatus.IN_PROGRESS
        self.started_at = datetime.now().isoformat()

    def complete(self, result: str = ""):
        self.status = StepStatus.COMPLETED
        self.result = result
        self.completed_at = datetime.now().isoformat()

    def fail(self, error: str = ""):
        self.status = StepStatus.FAILED
        self.error = error
        self.completed_at = datetime.now().isoformat()

    def skip(self, reason: str = ""):
        self.status = StepStatus.SKIPPED
        self.result = reason
        self.completed_at = datetime.now().isoformat()


@dataclass
class TaskPlan:
    goal: str
    steps: list[Step] = field(default_factory=list)
    current_step: int = 0
    status: PlanStatus = PlanStatus.PLANNING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    replanned: int = 0  # Quante volte è stato ri-pianificato

    @property
    def progress(self) -> str:
        """Progresso in formato leggibile."""
        done = sum(1 for s in self.steps if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED))
        total = len(self.steps)
        return f"{done}/{total}"

    @property
    def completed(self) -> bool:
        return all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED) for s in self.steps)

    @property
    def failed_steps(self) -> list[Step]:
        return [s for s in self.steps if s.status == StepStatus.FAILED]

    def summary_for_prompt(self) -> str:
        """Genera un riassunto del piano per il prompt LLM."""
        lines = [f"## PIANO ATTIVO: {self.goal}"]
        lines.append(f"Progresso: {self.progress}")

        for i, step in enumerate(self.steps):
            icon = {
                StepStatus.PENDING: "○",
                StepStatus.IN_PROGRESS: "●",
                StepStatus.COMPLETED: "✓",
                StepStatus.FAILED: "✗",
                StepStatus.SKIPPED: "−"
            }.get(step.status, "?")

            current = " ← ATTUALE" if i == self.current_step and step.status == StepStatus.IN_PROGRESS else ""
            lines.append(f"  {icon} Step {i+1}: {step.description}{current}")

            if step.result and step.status == StepStatus.COMPLETED:
                lines.append(f"    Risultato: {step.result[:100]}")
            elif step.error and step.status == StepStatus.FAILED:
                lines.append(f"    Errore: {step.error[:100]}")

        return "\n".join(lines)


class Planner:
    """Gestisce la pianificazione dei task complessi.

    La decomposizione in step avviene via LLM — il planner gestisce
    solo il tracking e l'aggiornamento degli step.
    """

    def create_plan(self, goal: str, steps: list[str] = None) -> TaskPlan:
        """Crea un piano dal goal e opzionali step."""
        plan = TaskPlan(goal=goal)
        if steps:
            for desc in steps:
                plan.steps.append(Step(description=desc))
        plan.status = PlanStatus.EXECUTING
        return plan

    def next_step(self, plan: TaskPlan) -> Optional[Step]:
        """Restituisce il prossimo step da eseguire."""
        for i, step in enumerate(plan.steps):
            if step.status == StepStatus.PENDING:
                plan.current_step = i
                step.start()
                return step
        return None

    def update_step(self, plan: TaskPlan, step_idx: int,
                    success: bool, result: str = "", error: str = ""):
        """Aggiorna lo stato di uno step."""
        if step_idx >= len(plan.steps):
            return

        step = plan.steps[step_idx]
        if success:
            step.complete(result)
        else:
            step.fail(error)

        # Aggiorna stato piano
        if plan.completed:
            plan.status = PlanStatus.COMPLETED
        elif plan.failed_steps and len(plan.failed_steps) >= 3:
            plan.status = PlanStatus.FAILED

    def add_step(self, plan: TaskPlan, description: str, after_current: bool = True):
        """Aggiunge uno step al piano."""
        step = Step(description=description)
        if after_current and plan.current_step < len(plan.steps):
            plan.steps.insert(plan.current_step + 1, step)
        else:
            plan.steps.append(step)

    def replan(self, plan: TaskPlan, new_steps: list[str]) -> TaskPlan:
        """Ri-pianifica mantenendo gli step completati."""
        completed = [s for s in plan.steps if s.status == StepStatus.COMPLETED]

        # Nuovi step
        new_step_objs = [Step(description=desc) for desc in new_steps]

        plan.steps = completed + new_step_objs
        plan.current_step = len(completed)
        plan.replanned += 1
        plan.status = PlanStatus.EXECUTING
        return plan

    def decomposition_prompt(self, goal: str, tools_available: list[str],
                             context: str = "") -> str:
        """Genera il prompt per far decomporre il task all'LLM."""
        tools_str = ", ".join(tools_available) if tools_available else "shell, file operations"

        return (
            f"Devo scomporre questo obiettivo in passi concreti e verificabili:\n\n"
            f"OBIETTIVO: {goal}\n\n"
            f"TOOL DISPONIBILI: {tools_str}\n\n"
            f"{'CONTESTO: ' + context if context else ''}\n\n"
            f"Rispondi SOLO con una lista numerata di passi. "
            f"Ogni passo deve essere un'azione concreta e verificabile. "
            f"Massimo 8 passi.\n\n"
            f"Formato:\n"
            f"1. Descrizione passo\n"
            f"2. Descrizione passo\n"
            f"..."
        )

    def parse_steps(self, llm_response: str) -> list[str]:
        """Parsa la risposta LLM per estrarre gli step."""
        import re
        steps = []
        for line in llm_response.strip().split("\n"):
            line = line.strip()
            # Match "1. something" or "- something"
            match = re.match(r'^(?:\d+[\.\)]\s*|-\s*)(.*)', line)
            if match:
                step_text = match.group(1).strip()
                if step_text and len(step_text) > 3:
                    steps.append(step_text)
        return steps
