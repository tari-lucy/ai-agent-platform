"""Explicit trusted wiring of a pinned OpenCode runner into the existing session driver.

Does not prepare a host, register capabilities, grant rights or start an agent.
The launcher supplies its verified template and the operator-selected output area.
"""

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from ai_agent_adapters.agent_harness import OpenCodeHarness
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.policy import Home, RunnerTemplate
from ai_agent_application.agent_workplace.ports import (
    ModelGateway,
    ModelReply,
    ModelRequest,
    WorkAdmission,
    WorkspaceGateway,
)
from ai_agent_application.control.ports import ResourceTicket
from ai_agent_contracts.agent_workplace import WRITABLE_AREAS
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode

from .work_session_egress import EgressSettings
from .work_session_egress import factory as egress_factory
from .work_session_home import OFFLINE as HOME_OFFLINE
from .work_session_home import HomeSettings
from .work_session_home import home as home_of
from .work_session_runtime import Release
from .workplace import Attempts

if TYPE_CHECKING:
    from .service import Execution, Runtime


MODEL_ONLY = "Модель доступна через шлюз платформы; это не общий доступ к сети, браузеру или игре. "


def environment_guide(
    output_area: str,
    helper_template: RunnerTemplate | None = None,
    helper_capability: str | None = None,
    *,
    home: bool = False,
    internet: bool = False,
) -> str:
    """Trusted adapter facts, separate from persona/canon and operator instructions."""
    from .work_session_egress import GUIDE as EGRESS_GUIDE
    from .work_session_home import GUIDE as HOME_GUIDE

    guide = (
        "\n\n## Устройство этой рабочей среды\n"
        "Управляемые области платформы не являются каталогами контейнера. "
        "Их содержимое и права узнавай через объявленные workspace MCP операции; "
        "не ищи каталог projects или knowledge на диске песочницы. "
        'list_files вызывай с arguments={"area":"knowledge"}, по одной области за вызов. '
        "area — точное логическое имя области из capabilities, без слеша, пути или списка имён. "
        "INVALID_ARGUMENTS означает неверные аргументы: исправь вызов. ACCESS_DENIED может "
        "означать отказ проверки файла или его оснований, а не только отсутствие права на область. "
        "Не утверждай, что нужен новый допуск, только на основании этого общего кода. "
        "write_note сохраняет заметку в knowledge. "
        "Для проектного результата используй объявленный файловый инструмент OpenCode: "
        "создай текстовый файл в /work/outputs/ (или outputs/ относительно /work). "
        "Имя файла должно быть коротким, lowercase ASCII без пробелов, например result.md; "
        "русский заголовок и содержание размещай внутри файла. "
        "Этот файл можно перечитать там же до окончания хода. "
        f"После успешного хода владелец файлов опубликует outputs в область {output_area}; "
        "название области задано настройкой, а не именем папки в контейнере. "
        "Не утверждай, что файл уже опубликован или действие выполнено, без результата "
        "инструмента. read_file поддерживает version, offset и length (до 4000 символов); "
        "для следующей части используй next_offset и сверяй checksum/version. "
        "Бинарные материалы сначала оцени по метаданным: текстовый инструмент их не разбирает. "
        "Пустой список файлов — корректный результат, а не запрос новых прав. "
        "Доводи поручение до результата доступными средствами самостоятельно: "
        "читать материалы, писать и исправлять скрипты, сохранять заметки и результаты "
        "в уже разрешённых областях можно без согласования каждого подшага. "
        "Не спрашивай оператора, читать ли нужный для поручения файл. "
        "Наличие инструмента не выдаёт права; capability и доступность сверяй через capabilities. "
        "Новый доступ, отсутствующий инструмент или неустранимую проблему запрашивай одним "
        "конкретным обращением, а не повторным запросом на каждый шаг. "
        "В ответе отличай прочитанное от предположений и проверенных действий. "
        "Ссылка на материал в контексте означает наличие материала, а не чтение его байтов. "
        "Имя архива, перечень файлов и строки из бинарника не доказывают устройство протокола. "
        "Технический вывод из архива подкрепляй именем записи и извлечённым значением; "
        "если код не исследован, не пиши «по коду» и не придумывай порт или команды. "
        "Текстовый read_file не декодирует ZIP/JAR: для содержимого нужен разбор архивных байтов. "
        "Результат можно объяснить прямо в чате; файл создавай, когда нужен сохраняемый артефакт "
        "или об этом попросили. "
        "Рабочий контейнер и Python-контейнер изолированы от Windows и друг от друга. "
        "localhost/127.0.0.1 в контейнере — сам этот контейнер, не компьютер оператора. "
        + MODEL_ONLY
    )
    if home:
        guide += HOME_GUIDE
        if internet:
            # The home text says "no network yet"; this turn's gateway supersedes it.
            guide = guide.replace(HOME_OFFLINE, "")
    if internet:
        guide = guide.replace(MODEL_ONLY, "Модель доступна через шлюз платформы. ")
        guide += EGRESS_GUIDE
    if helper_template is None or helper_capability is None:
        return guide + "Python helper в этом запуске не подключён; не обещай его выполнение."
    limits = helper_template.ceiling
    return guide + (
        f"Python helper подключён как возможность {helper_capability}; это ещё не Permission. "
        "Если право уже действует для нужных областей, используй helpers scripts самостоятельно: "
        "save_script сохраняет Python, run_script запускает его отдельным контейнером без сети. "
        "Доступна стандартная библиотека, в том числе zipfile для ZIP/JAR и json для метаданных. "
        "Устанавливать пакеты, подключаться к WebSocket/Minecraft/Telegram или путям Windows "
        "через Python нельзя: написанный клиент не меняет сетевой запрет. "
        "inputs принимает JSON-массив точных {id,version} файлов владельца; передай туда архив. "
        "Скрипт — inputs/input-0000, данные начинаются с inputs/input-0001; "
        "inputs/manifest.json содержит соответствие ссылок. argv — JSON-массив строк. "
        "Для архива сначала изучи оглавление и размеры, затем читай нужные небольшие записи; "
        "не запускай код архива и не распаковывай всё без необходимости. "
        "Пиши выводы под outputs/, затем читай файлы из receipt штатным read_file. "
        "Только receipt run_script подтверждает исполнение; сохранение скрипта его не запускает. "
        f"Потолки этого helper: wall {limits.wall_seconds} с, CPU {limits.cpu_seconds} с, "
        f"RAM {limits.memory_mb} МиБ, вход {helper_template.max_input_bytes} байт, "
        f"выход {limits.output_bytes} байт. STOP отзывает исполнение; "
        "неизвестный исход не повторяй вслепую."
    )


class EnvironmentModels:
    def __init__(
        self,
        delegate: ModelGateway,
        output_area: str,
        helper_template: RunnerTemplate | None = None,
        helper_capability: str | None = None,
        *,
        home: bool = False,
        internet: bool = False,
    ) -> None:
        self.delegate = delegate
        self.guide = environment_guide(
            output_area, helper_template, helper_capability, home=home, internet=internet
        )

    async def generate(self, admission: WorkAdmission, request: ModelRequest) -> ModelReply:
        if not request.messages or request.messages[0].role != "system":
            return ModelReply("not_admitted", "", "")
        first = replace(request.messages[0], content=request.messages[0].content + self.guide)
        return await self.delegate.generate(
            admission, replace(request, messages=(first, *request.messages[1:]))
        )


class OpenCodeSessions:
    def __init__(
        self,
        runtime: Runtime,
        template: RunnerTemplate,
        *,
        output_area: str,
        helper_template: RunnerTemplate | None = None,
        helper_capability: str | None = None,
        prewarm_enabled: bool = False,
        prewarm_idle_seconds: float = 120,
        resident_enabled: bool = False,
        resident_idle_seconds: float = 1800,
        home: HomeSettings | None = None,
        egress: EgressSettings | None = None,
    ) -> None:
        from .work_session_helpers import PREFIX as HELPER_PREFIX

        if (
            template.executable_prefix != PREFIX
            or template.model_role_id != "activity"
            or output_area not in WRITABLE_AREAS
        ):
            raise ValueError("SESSION_RUNNER_TEMPLATE_INVALID")
        if (
            bool(template.max_helper_calls) != (helper_template is not None)
            or (helper_template is None) != (helper_capability is None)
            or helper_template is not None
            and (
                helper_template.id != template.id
                or helper_template.executable_prefix != HELPER_PREFIX
                or helper_template.model_role_id is not None
                or helper_template.max_files > 16
                or helper_capability == template.id
                or helper_template.ceiling.wall_seconds >= template.ceiling.wall_seconds
            )
        ):
            raise ValueError("SESSION_HELPER_TEMPLATE_INVALID")
        if runtime.workplace is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        self.rt, self.template, self.output_area = runtime, template, output_area
        self.helper_template, self.helper_capability = helper_template, helper_capability
        self.home = home
        self.egress = egress
        # Live handles only, not another durable job/session/resource journal.
        self.runners: dict[str, DockerTaskRunner] = {}
        self.helper_runners: dict[str, DockerTaskRunner] = {}
        self.resources: dict[str, ResourceTicket] = {}
        from .work_session_warm import WarmSessions

        self.warm = (
            WarmSessions(
                runtime,
                template,
                idle_seconds=prewarm_idle_seconds,
                helper=helper_template is not None,
                home=self._home if home is not None else None,
                resident=resident_enabled,
                resident_idle_seconds=resident_idle_seconds,
            )
            if prewarm_enabled or resident_enabled
            else None
        )

    def install(self) -> None:
        if self.rt.session_harness is not None:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        self.rt.session_harness = self.create
        self.rt.session_capability = self.template.id
        self.rt.session_output_area = self.output_area
        self.rt.session_helper_capability = self.helper_capability
        self.rt.session_egress_capability = self.egress.capability_id if self.egress else None
        if self.home is not None:
            from ai_agent_adapters.sandbox.home_files import HomeFiles

            self.rt.session_home_files = HomeFiles(self.template.image_id)
        self.rt.session_prewarmer = self.warm

    async def create(
        self, execution: Execution, models: ModelGateway, admission: WorkAdmission
    ) -> OpenCodeHarness:
        from .activity import tools

        rt = self.rt
        if (
            execution.runtime is not rt
            or execution.args.request.work_session_id is None
            or execution.command.operation_id != admission.operation_id
            or execution.command.scope != admission.scope
            or admission.attempt_id in self.runners
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        place = rt.workplace
        if place is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)

        internet: str | None = None  # this turn's internet capability, decided below

        async def authorize(gate: WorkAdmission, capability: str) -> None:
            if gate != admission or capability != self.template.id:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            # The runner periodically checks this fence, including DURING model IO;
            # its cheap epoch/STOP check remains independent at 100 ms.
            # Full JobOwner proof stays at create/model/file boundaries; acquiring its
            # lock again here can contend with that same admitted operation.
            execution.guard()
            usable = {
                tool.manifest.capability_id
                for tool in await tools(rt, gate.scope, session=True)
                if tool.available
            }
            # A turn with the gateway ends when the internet permission is revoked.
            if capability not in usable or internet is not None and internet not in usable:
                raise ContractError(ErrorCode.ACCESS_DENIED)

        async def lifecycle(
            gate: WorkAdmission, name: str, phase: Literal["register", "removed", "unknown"]
        ) -> None:
            if gate != admission:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if phase == "register":
                await authorize(gate, self.template.id)
                token = rt.supervisor.resolve(
                    execution.command.cancellation_id,
                    scope=gate.scope,
                    freshness=gate.freshness,
                )
                self.resources[name] = rt.supervisor.register(token, name)
            elif phase == "removed":
                ticket = self.resources.pop(name, None)
                if ticket is None:
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                # A real Docker removal receipt is accepted even after STOP/revocation.
                rt.supervisor.acknowledge(ticket)
            else:
                # No receipt: retain the resource/runner for trusted reconciliation.
                rt.supervisor.critical_failure(ErrorCode.OUTCOME_UNKNOWN)

        helpers = None
        if self.helper_template is not None:
            from .work_session_helpers import Helpers
            from .workplace import granted_areas

            helper_areas = {self.output_area}

            async def authorize_helper(gate: WorkAdmission, capability: str) -> None:
                await authorize(gate, capability)
                assert self.helper_capability is not None
                if not helper_areas <= await granted_areas(rt, gate.scope, self.helper_capability):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if not any(
                    t.available and t.manifest.capability_id == self.helper_capability
                    for t in await tools(rt, gate.scope, session=True)
                ):
                    raise ContractError(ErrorCode.ACCESS_DENIED)

            async def helper_lifecycle(
                gate: WorkAdmission, name: str, phase: Literal["register", "removed", "unknown"]
            ) -> None:
                if phase == "register":
                    await authorize_helper(gate, self.template.id)
                await lifecycle(gate, name, phase)

            helper_runner = DockerTaskRunner(
                (self.helper_template,),
                place.files,
                Attempts(rt),
                Release(rt),
                authorize=authorize_helper,
                lifecycle=helper_lifecycle,
            )
            self.helper_runners[admission.attempt_id] = helper_runner
            helpers = Helpers(
                execution, admission, self.helper_template, helper_runner, helper_areas
            )

        await execution.verify()
        await authorize(admission, self.template.id)
        if self.egress is not None and any(
            tool.available and tool.manifest.capability_id == self.egress.capability_id
            for tool in await tools(rt, admission.scope, session=True)
        ):
            internet = self.egress.capability_id
        home = (
            self._home(admission.scope, internet=internet is not None)
            if self.home is not None
            else None
        )
        runner = DockerTaskRunner(
            (self.template,),
            place.files,
            Attempts(rt),
            Release(rt),
            authorize=authorize,
            lifecycle=lifecycle,
            models=EnvironmentModels(
                models,
                self.output_area,
                self.helper_template,
                self.helper_capability,
                home=home is not None,
                internet=internet is not None,
            ),
            workspace=self._workspace(execution, admission)
            if self.template.max_workspace_calls
            else None,
            helpers=helpers,
            # A prewarmed sandbox has no network: a turn with the gateway starts cold.
            prepared=await self.warm.take(home.volume if home else None)
            if self.warm is not None and internet is None
            else None,
            home=home,
            egress=egress_factory(self.egress, admission)
            if self.egress is not None and internet is not None
            else None,
        )
        self.runners[admission.attempt_id] = runner
        controller = self

        class TrackedHarness(OpenCodeHarness):
            async def close(self, session_key: str) -> None:
                nested = controller.helper_runners.get(admission.attempt_id)
                if nested is not None:
                    await nested.cancel(admission)
                await super().close(session_key)
                if session_key == execution.args.request.work_session_id:
                    controller.runners.pop(admission.attempt_id, None)
                    controller.helper_runners.pop(admission.attempt_id, None)

        return TrackedHarness(runner, self.template.id, self.template.ceiling)

    def _home(self, scope: Scope, *, internet: bool = False) -> Home:
        assert self.home is not None
        facts = environment_guide(
            self.output_area,
            self.helper_template,
            self.helper_capability,
            home=True,
            internet=internet,
        )
        return home_of(self.home, scope, facts)

    @staticmethod
    def _workspace(execution: Execution, admission: WorkAdmission) -> WorkspaceGateway:
        from .work_session_workspace import Workspace

        return Workspace(execution, admission)
