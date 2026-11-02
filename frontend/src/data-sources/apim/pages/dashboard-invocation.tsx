import { useEffect, useMemo, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, ChevronRight, Code2, Cpu, Image as ImageIcon, ListChecks, MessageSquare, RefreshCw, Send } from "lucide-react"

import { FoundryLogo, MicrosoftLogo } from "../../../components/brand-logos"
import { ClientSampleCode } from "../../../components/finops/application-client-quickstart"
import { ModelVendorLogo, modelVendorFromMetadata } from "../../../components/model-management/model-vendor-select"
import { ButtonGroup } from "../../../components/ui/button-group"
import { Input } from "../../../components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select"
import { dataSource } from "../api"
import { ApiError } from "../../../api/client"
import { finopsKeys, finopsQueries, invalidateFinOps } from "../queries"
import { useAuth } from "../../../providers/auth-provider"
import { gatewayInvocationChoices, personMayInvoke, resolveInvocationModel, type GatewaySelection } from "../../../lib/gateway-topology"
import { modelInventoryFamily } from "../../../lib/model-inventory-filters"
import { clientSampleFor, type ClientSampleOperation, type ClientSampleRequest } from "../../../lib/application-client-samples"
import type { DecisionInvocationRequest, DecisionInvocationResponse, EnterpriseEntityCatalog, ImageGenerationOptions, ImageInvocationResponse, ManagedModel, ModelInvocationResponse, ModelRuntime } from "../types"
import { ImageGenerationResult, ImageOptions } from "./image-generation-result"
import {
  compact,
  EmptyState,
  ErrorState,
  FilterSelect,
  formatLatency,
  LoadingState,
  PanelTitle,
  queryError,
} from "./dashboard-shared"

const DEFAULT_MODEL_OPTION = "__default__"
const MODEL_TARGET_PREFIX = "model:"
const ROUTER_TARGET_PREFIX = "router:"
const INVOCATION_MAX_OUTPUT_TOKENS = 1_024
const CHAT_PROMPT_SAMPLE = "Return one short recommendation for reducing model cost."
const DECISION_STATE_SAMPLE = "The checkout service has returned HTTP 500 errors for 15 minutes. Customers cannot complete purchases, and the issue affects all regions."
const DECISION_STATE_JSON_SAMPLE = JSON.stringify({
  service: "checkout",
  error: "HTTP 500",
  duration_minutes: 15,
  impact: "Customers cannot complete purchases in any region",
}, null, 2)

type InvocationDraft = { owner: string; mode: "chat" | "image" | "decision"; prompt: string; stateFormat: "text" | "json"; questions: string }
let invocationDraft: InvocationDraft | null = null

const INVOCATION_DENIALS = new Map([
  ["model_not_assigned", "你尚未获得该模型的使用权限，请联系管理员分配。"],
  ["budget_exceeded", "本月预算已用完，调用已被拦截。"],
  ["image_budget_insufficient", "剩余预算不足以预留本次图像生成所需额度。"],
])

function InvocationModelLogo({ model, runtime }: { model: ManagedModel; runtime?: ModelRuntime }) {
  if (model.capabilities.includes("decision")) return <MicrosoftLogo size={16} />
  if (runtime?.runtime_kind === "foundry" && model.upstream_model_id === "model-router") return <FoundryLogo size={16} />
  if (runtime?.runtime_kind === "openai_compatible" && runtime.config.api_format === "openai_chat") {
    return <ModelVendorLogo value={modelVendorFromMetadata(runtime.config, `${model.model_key} ${model.display_name} ${runtime.name}`)} size={16} />
  }
  const family = modelInventoryFamily(model)
  return family === "generic" ? <Cpu aria-hidden="true" size={16} /> : <ModelVendorLogo value={family === "claude" ? "anthropic" : family} size={16} />
}

export function AgentInvocation({ entities }: { entities: EnterpriseEntityCatalog }) {
  const queryClient = useQueryClient();
  const { user: sessionUser } = useAuth();
  const [draft] = useState(() => sessionUser && invocationDraft?.owner === sessionUser.email ? invocationDraft : null);
  const registry = useQuery(finopsQueries.registry());
  const [departmentId, setDepartmentId] = useState(
    entities.departments[0]?.id ?? "",
  );
  const projects = entities.projects.filter(
    (item) => item.parent_id === departmentId,
  );
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const agents = entities.agents.filter((item) => item.parent_id === projectId);
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  const users = useMemo(() => {
    if (!sessionUser) return [];
    if (sessionUser.role === "owner" && entities.users.some((person) => person.id === sessionUser.email)) {
      return entities.users;
    }
    const sessionIdentity = {
      id: sessionUser.email,
      name: sessionUser.name ?? sessionUser.email,
      parent_id: null,
    };
    if (sessionUser.role === "owner") return [sessionIdentity, ...entities.users];
    return [
      sessionIdentity,
      ...(entities.invocation_testers ?? []).filter(
        (person) => person.parent_id === departmentId && person.id !== sessionUser.email,
      ),
    ];
  }, [departmentId, entities.invocation_testers, entities.users, sessionUser]);
  const [userId, setUserId] = useState(sessionUser?.email ?? users[0]?.id ?? "");
  useEffect(() => {
    if (users.some((person) => person.id === userId)) return;
    setUserId(sessionUser?.email ?? users[0]?.id ?? "");
  }, [sessionUser?.email, userId, users]);
  const [mode, setMode] = useState<"chat" | "image" | "decision">(draft?.mode ?? "chat");
  const imageMode = mode === "image";
  const decisionMode = mode === "decision";
  const assignedRouters = useQuery({ ...finopsQueries.assignedModelRouters(userId), enabled: mode === "chat" && Boolean(userId) });
  const [invocationTarget, setInvocationTarget] = useState(DEFAULT_MODEL_OPTION);
  const [gatewaySelection, setGatewaySelection] = useState<GatewaySelection>({ targetId: null, deploymentId: null });
  const selectedDirectModelId = invocationTarget.startsWith(MODEL_TARGET_PREFIX)
    ? invocationTarget.slice(MODEL_TARGET_PREFIX.length) : "";
  const explicitGatewayAvailable = registry.data?.explicit_gateway_selection_supported === true;
  const topology = useQuery({ ...finopsQueries.apimTargetTopologies(), enabled: explicitGatewayAvailable && Boolean(selectedDirectModelId) });
  const invocationGateways = registry.data && selectedDirectModelId
    ? gatewayInvocationChoices(registry.data.gateways, topology.data ?? null, selectedDirectModelId)
      .filter(choice => choice.kind === "managed" || registry.data?.self_hosted_gateway_enabled === true)
    : [];
  const selectedGateway = invocationGateways.find(choice => choice.targetId === gatewaySelection.targetId && choice.deploymentId === gatewaySelection.deploymentId);
  const explicitGateway = gatewaySelection.targetId !== null;
  const observedGatewayStream = topology.data?.items.some(item =>
    item.target.gateway_profile_id === gatewaySelection.targetId
    && item.target.capabilities.response_observer === "supported",
  ) === true;
  const gatewayReady = !explicitGateway || (explicitGatewayAvailable && !topology.isLoading && !topology.error && Boolean(selectedGateway?.selectable));
  const [imageOptions, setImageOptions] = useState<ImageGenerationOptions | null>(null);
  const policyPeriod = new Date().toISOString().slice(0, 7);
  const modelPolicies = useQuery({
    queryKey: [...finopsKeys.budgets(policyPeriod), "allocation-preview"],
    queryFn: () => dataSource.budgets(policyPeriod, true),
    enabled: Boolean(userId),
  });
  const invocationPerson = modelPolicies.data?.items.find((item) => item.scope_type === "user" && item.scope_id === userId);
  const assignedModelIds = invocationPerson?.model_policy_configured ? new Set(invocationPerson.allowed_model_ids) : null;
  const invocableModels = (registry.data?.models ?? []).filter((model) => personMayInvoke(model, assignedModelIds));
  const invocationModel = resolveInvocationModel(
    invocableModels,
    invocationTarget === DEFAULT_MODEL_OPTION ? null : selectedDirectModelId,
    imageMode,
    decisionMode,
  );
  const selectedModelUnavailable = invocationTarget.startsWith(MODEL_TARGET_PREFIX) && !invocationModel;
  const imageProfile = imageMode && invocationTarget.startsWith(MODEL_TARGET_PREFIX)
    ? registry.data?.models.find(model => model.id === invocationTarget.slice(MODEL_TARGET_PREFIX.length))?.image_profile
    : null;
  const effectiveImageOptions = imageProfile ? imageOptions ?? {} : null;
  const [imageResult, setImageResult] = useState<ImageInvocationResponse | null>(null);
  const [prompt, setPrompt] = useState(draft?.prompt ?? CHAT_PROMPT_SAMPLE);
  const [stateFormat, setStateFormat] = useState<"text" | "json">(draft?.stateFormat ?? "text");
  const [questions, setQuestions] = useState(draft?.questions ?? JSON.stringify({
    urgent: { type: "noul", instructions: "Does this incident need urgent attention?" },
    team: { type: "choice", instructions: "Which team should handle this incident?",
      criteria: { engineering: "Bugs, errors, and outages", support: "How-to and account questions" } },
    severity: { type: "score", instructions: "How severe is this incident?",
      criteria: ["Minor", "Major", "Critical"] },
  }, null, 2));
  useEffect(() => {
    if (sessionUser) invocationDraft = { owner: sessionUser.email, mode, prompt, stateFormat, questions };
  }, [sessionUser, mode, prompt, stateFormat, questions]);
  const [outputLimit, setOutputLimit] = useState(String(INVOCATION_MAX_OUTPUT_TOKENS));
  const maxOutputTokens = Number(outputLimit);
  const validOutputLimit = Number.isSafeInteger(maxOutputTokens) && maxOutputTokens >= 1 && maxOutputTokens <= 1_000_000;
  const [result, setResult] = useState<((ModelInvocationResponse | DecisionInvocationResponse) & { requested_max_output_tokens?: number }) | null>(null);
  const [isInvoking, setIsInvoking] = useState(false);
  const [invokeError, setInvokeError] = useState<string | null>(null);
  const [sessionId] = useState(() => crypto.randomUUID());
  const selectUser = (value: string | undefined) => {
    setUserId(value ?? "");
    if (invocationTarget.startsWith(ROUTER_TARGET_PREFIX)) {
      setInvocationTarget(DEFAULT_MODEL_OPTION);
      setResult(null);
    }
  };
  const selectDepartment = (value: string | undefined) => {
    const nextDepartmentId = value ?? "";
    const nextProjects = entities.projects.filter(
      (item) => item.parent_id === nextDepartmentId,
    );
    const nextProjectId = nextProjects[0]?.id ?? "";
    const nextAgentId =
      entities.agents.find((item) => item.parent_id === nextProjectId)?.id ??
      "";
    setDepartmentId(nextDepartmentId);
    setProjectId(nextProjectId);
    setAgentId(nextAgentId);
    setUserId(sessionUser?.email ?? "");
  };
  const selectProject = (value: string | undefined) => {
    const nextProjectId = value ?? "";
    setProjectId(nextProjectId);
    setAgentId(
      entities.agents.find((item) => item.parent_id === nextProjectId)?.id ??
        "",
    );
  };
  const invoke = async () => {
    if (!registry.data || !gatewayReady || isInvoking || (mode === "chat" && !validOutputLimit)) return;
    const routerId = mode === "chat" && invocationTarget.startsWith(ROUTER_TARGET_PREFIX)
      ? invocationTarget.slice(ROUTER_TARGET_PREFIX.length)
      : null;
    const model = invocationModel;
    if (!model && !routerId) return;
    if (imageMode && (registry.data.image_generation_supported !== true || registry.data.image_configuration_schema_version !== 4 || !model || !imageProfile || !effectiveImageOptions)) return;
    if (decisionMode && (registry.data.decision_supported !== true || !model)) return;
    const runtime = registry.data.runtimes.find(
      (item) => item.id === model?.runtime_id,
    );
    const project = entities.projects.find((item) => item.id === projectId)!;
    const department = entities.departments.find(
      (item) => item.id === departmentId,
    )!;
    const agent = entities.agents.find((item) => item.id === agentId)!;
    const invocationUser = users.find((item) => item.id === userId)!;
    setIsInvoking(true);
    setInvokeError(null);
    setResult(null);
    setImageResult(null);
    try {
      const invocation = {
        ...(routerId
          ? {
              router_id: routerId,
              router_context: {
                task_type: `${project.id}-interactive`,
                request_parameter: prompt,
                user_id: invocationUser.id,
                required_capability: "chat",
                session_id: sessionId,
              },
            }
            : { runtime_id: explicitGateway ? undefined : model!.runtime_id, model_id: model!.id,
              gateway_profile_id: explicitGateway ? gatewaySelection.targetId! : undefined,
              gateway_deployment_id: explicitGateway ? gatewaySelection.deploymentId! : undefined,
              router_context: { session_id: sessionId } }),
        metadata: {
          organization_id: entities.organizations[0].id,
          organization: entities.organizations[0].name,
          department_id: department.id,
          department: department.name,
          project_id: project.id,
          project: project.name,
          agent_id: agent.id,
          agent: agent.name,
          user_id: invocationUser.id,
          user: invocationUser.name,
          workflow: decisionMode ? "decision" : imageMode ? "image-generation" : `${project.id}-interactive`,
          model_id: routerId ?? model!.id,
          // These two are display values, so they must carry names. Sending the registry
          // UUID here put a bare UUID in the model column of every denial trace; the
          // ingestion path hid it by resolving the identity server-side, but a caller
          // should not be labelling a model with its primary key in the first place.
          // The runtime had the same defect, and its fallback still reintroduced it, so a
          // missing registry entry now degrades to unattributed rather than to a UUID.
          model: routerId
            ? assignedRouters.data?.items.find((item) => item.id === routerId)?.name ?? "model-router"
            : model!.model_key,
          runtime: routerId ? "model-router" : runtime?.name ?? "unattributed",
          request_source: decisionMode ? "decision-invocation-module" : imageMode ? "image-invocation-module" : "agent-invocation-module",
          run_id: crypto.randomUUID(),
          turn_index: 1,
        },
        messages: [{ role: "user", content: prompt }],
        max_output_tokens: maxOutputTokens,
        stream: !explicitGateway || observedGatewayStream,
      };
      if (imageMode && model && imageProfile && effectiveImageOptions) {
        const response = await dataSource.generateImage({
          ...effectiveImageOptions, model_id: model.id, runtime_id: explicitGateway ? undefined : model.runtime_id,
          gateway_profile_id: explicitGateway ? gatewaySelection.targetId! : undefined,
          gateway_deployment_id: explicitGateway ? gatewaySelection.deploymentId! : undefined,
          metadata: invocation.metadata, prompt, n: 1, stream: false,
        });
        setImageResult(response);
      } else if (decisionMode && model) {
        const response = await dataSource.invokeDecision({
          model_id: model.id, runtime_id: explicitGateway ? undefined : model.runtime_id,
          gateway_profile_id: explicitGateway ? gatewaySelection.targetId! : undefined,
          gateway_deployment_id: explicitGateway ? gatewaySelection.deploymentId! : undefined,
          metadata: invocation.metadata,
          state: stateFormat === "json" ? JSON.parse(prompt) : prompt,
          questions: JSON.parse(questions) as DecisionInvocationRequest["questions"],
        });
        setResult(response);
      } else {
        const response = await dataSource.invokeModel(invocation);
        setResult({ ...response, requested_max_output_tokens: maxOutputTokens });
      }
      void invalidateFinOps(queryClient);
    } catch (error) {
      setInvokeError((error instanceof ApiError && error.detail ? INVOCATION_DENIALS.get(error.detail) : undefined) ?? queryError(error));
    } finally {
      setIsInvoking(false);
    }
  };
  if (registry.isLoading) return <LoadingState label="正在加载调用配置" />;
  if (registry.error) return <ErrorState error={registry.error} />;
  const models = invocableModels.filter((item) => item.enabled && item.capabilities.includes("image_generation") === imageMode
    && item.capabilities.includes("decision") === decisionMode);
  const selectedModel = invocationTarget.startsWith(MODEL_TARGET_PREFIX)
    ? invocationModel
    : null;
  const selectedRouter = invocationTarget.startsWith(ROUTER_TARGET_PREFIX)
    ? assignedRouters.data?.items.find((router) => router.id === invocationTarget.slice(ROUTER_TARGET_PREFIX.length))
    : null;
  const totalTokens = result?.usage
    ? result.usage.input_tokens +
      result.usage.cached_tokens +
      result.usage.output_tokens
    : null;
  const cacheWriteTokens = result?.usage?.cache_write_tokens ?? 0;
  const cacheReadTokens = result?.usage
    ? Math.max(result.usage.cached_tokens - cacheWriteTokens, 0)
    : null;
  const exhaustedOutputBudget = mode === "chat" && !!result?.usage && result.usage.output_tokens >= (result.requested_max_output_tokens ?? 64);
  const canInvoke = !isInvoking && gatewayReady && Boolean(invocationModel || selectedRouter) && (mode !== "chat" || validOutputLimit) && Boolean(prompt.trim() && userId && projectId && agentId)
    && (!decisionMode || (registry.data?.decision_supported === true && Boolean(questions.trim())))
    && (!imageMode || (registry.data?.image_generation_supported === true && registry.data.image_configuration_schema_version === 4 && Boolean(selectedModel) && Boolean(imageProfile && effectiveImageOptions)));
  const sampleRuntime = registry.data?.runtimes.find(runtime => runtime.id === invocationModel?.runtime_id);
  const sampleGateway = sampleRuntime?.gateway_profile_id
    ? registry.data?.gateways.find(gateway => gateway.id === sampleRuntime.gateway_profile_id)
    : registry.data?.gateways.find(gateway => gateway.is_default);
  const sampleTarget = registry.data?.apim_target_topology?.items.find(target => target.target.gateway_profile_id === sampleGateway?.id);
  const sampleEndpoint = (explicitGateway ? selectedGateway?.endpoint
    : sampleGateway?.enabled && sampleGateway.implementation === "apim" ? sampleTarget?.gateway_endpoint ?? sampleGateway.base_url : null)?.replace(/\/+$/, "") ?? "";
  const sampleModelKey = selectedRouter?.name ?? invocationModel?.model_key ?? "";
  const sampleOperation: ClientSampleOperation = decisionMode ? "decision" : imageMode ? "image_generations" : "chat_completions";
  let sampleRequest: ClientSampleRequest | undefined;
  let sampleError: string | null = null;
  if (sampleModelKey && (mode !== "chat" || validOutputLimit)) {
    try {
      const payload = decisionMode ? {
        model: sampleModelKey,
        state: stateFormat === "json" ? JSON.parse(prompt) : prompt,
        questions: JSON.parse(questions),
      } : imageMode ? {
        ...effectiveImageOptions, model: sampleModelKey, prompt, n: 1, stream: false,
      } : {
        model: sampleModelKey, messages: [{ role: "user", content: prompt }],
        [String(sampleRuntime?.config.max_tokens_field ?? "max_tokens")]: maxOutputTokens,
        stream: false,
      };
      sampleRequest = { payload, headers: {
        "x-org-id": entities.organizations[0]?.id ?? "",
        "x-department-id": departmentId, "x-project-id": projectId,
        "x-agent-id": agentId, "x-user-id": userId,
      } };
    } catch (error) {
      sampleError = queryError(error);
    }
  }
  const selectMode = (next: typeof mode) => {
    if (next === mode || isInvoking) return;
    setMode(next);
    setImageOptions(null);
    setInvocationTarget(DEFAULT_MODEL_OPTION);
    setGatewaySelection({ targetId: null, deploymentId: null });
    setResult(null);
    setImageResult(null);
    setInvokeError(null);
    setPrompt(next === "decision" ? (stateFormat === "json" ? DECISION_STATE_JSON_SAMPLE : DECISION_STATE_SAMPLE) : next === "chat" ? CHAT_PROMPT_SAMPLE : "");
  };
  const selectStateFormat = (next: typeof stateFormat) => {
    setStateFormat(next);
    if (!prompt.trim() || prompt === DECISION_STATE_SAMPLE || prompt === DECISION_STATE_JSON_SAMPLE) {
      setPrompt(next === "json" ? DECISION_STATE_JSON_SAMPLE : DECISION_STATE_SAMPLE);
    }
  };
  return (
    <div className="invoke-layout">
      <section className="finops-panel">
        <PanelTitle title="调用参数" meta="在线模型" />
        <div className="invoke-modebar"><ButtonGroup className="usage-metric-segment" aria-label="调用模式">
          <button type="button" className={mode === "chat" ? "active" : ""} aria-pressed={mode === "chat"} disabled={isInvoking} onClick={() => selectMode("chat")}><MessageSquare size={14} />对话</button>
          <button type="button" className={imageMode ? "active" : ""} aria-pressed={imageMode} disabled={isInvoking} onClick={() => selectMode("image")}><ImageIcon size={14} />文生图</button>
          {registry.data?.decision_supported === true && <button type="button" className={decisionMode ? "active" : ""} aria-pressed={decisionMode} disabled={isInvoking} onClick={() => selectMode("decision")}><ListChecks size={14} />Decision</button>}
        </ButtonGroup></div>
        <div className="invoke-form">
          <FilterSelect
            label="部门"
            value={departmentId}
            items={entities.departments}
            onChange={selectDepartment}
          />
          <FilterSelect
            label="项目"
            value={projectId}
            items={projects}
            onChange={selectProject}
          />
          <FilterSelect
            label="智能体"
            value={agentId}
            items={agents}
            onChange={(value) => setAgentId(value ?? "")}
          />
          <FilterSelect
            label="用户"
            value={userId}
            items={users}
            onChange={selectUser}
            allowAll={false}
          />
          <div className="invoke-select">
            <span>调用目标</span>
            <Select
              value={invocationTarget}
              disabled={isInvoking}
              onValueChange={(next) => { setInvocationTarget(next ?? DEFAULT_MODEL_OPTION); setImageOptions(null); setGatewaySelection({ targetId: null, deploymentId: null }); if (mode === "chat" && !prompt.trim()) setPrompt(CHAT_PROMPT_SAMPLE); }}
            >
              <SelectTrigger aria-label="调用目标" title={selectedModelUnavailable ? "所选模型不可用" : selectedRouter?.name ?? selectedModel?.display_name ?? "默认可用模型"}>
                <SelectValue>
                  {selectedModel && !selectedModelUnavailable ? <span className="invoke-model-option">
                    <InvocationModelLogo model={selectedModel} runtime={registry.data?.runtimes.find(runtime => runtime.id === selectedModel.runtime_id)} />
                    <span>{selectedModel.display_name}</span>
                  </span> : selectedModelUnavailable ? "所选模型不可用" : selectedRouter ? `${selectedRouter.name} · Router v${selectedRouter.version}` : (decisionMode ? "选择 Decision 模型" : imageMode ? "选择图片模型" : "默认可用模型")}
                </SelectValue>
              </SelectTrigger>
              <SelectContent className="invoke-target-menu" align="start" alignItemWithTrigger={false}>
                <SelectItem value={DEFAULT_MODEL_OPTION}>
                  {decisionMode ? "选择 Decision 模型" : imageMode ? "选择图片模型" : "默认可用模型"}
                </SelectItem>
                <SelectSeparator />
                <SelectGroup><SelectLabel>直接模型</SelectLabel>{models.map((model) => (
                  <SelectItem key={model.id} value={`${MODEL_TARGET_PREFIX}${model.id}`}>
                    <span className="invoke-model-option">
                      <InvocationModelLogo model={model} runtime={registry.data?.runtimes.find(runtime => runtime.id === model.runtime_id)} />
                      <span>{model.display_name}</span>
                      {models.some((other) => other.id !== model.id && other.display_name === model.display_name) && <small data-no-localize>{model.model_key}</small>}
                    </span>
                  </SelectItem>
                ))}</SelectGroup>
                {mode === "chat" && (assignedRouters.data?.items.length ?? 0) > 0 && <><SelectSeparator /><SelectGroup><SelectLabel>模型路由器</SelectLabel>{assignedRouters.data!.items.map((router) => (
                  <SelectItem key={router.id} value={`${ROUTER_TARGET_PREFIX}${router.id}`}>
                    {router.name} · v{router.version}
                  </SelectItem>
                ))}</SelectGroup></>}
              </SelectContent>
            </Select>
            {selectedModelUnavailable && <div className="invoke-error" role="alert">所选模型不可用</div>}
            {mode === "chat" && assignedRouters.isFetching && <small className="invoke-target-loading"><RefreshCw className="spin" size={11} />正在读取人员路由器</small>}
          </div>
          {explicitGatewayAvailable && selectedDirectModelId && <>
            <div className="invoke-select">
              <span>APIM 实例</span>
              <Select value={gatewaySelection.targetId ?? DEFAULT_MODEL_OPTION} disabled={isInvoking || topology.isLoading || Boolean(topology.error)} onValueChange={value => {
                const targetId = value && value !== DEFAULT_MODEL_OPTION ? value : null;
                const first = invocationGateways.find(choice => choice.targetId === targetId && choice.selectable);
                setGatewaySelection({ targetId, deploymentId: first?.deploymentId ?? null });
                setInvokeError(null);
              }}>
                <SelectTrigger aria-label="APIM 实例"><SelectValue>{explicitGateway ? registry.data?.gateways.find(item => item.id === gatewaySelection.targetId)?.name ?? "选择 APIM 实例" : "默认路由"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  <SelectItem value={DEFAULT_MODEL_OPTION}>默认路由</SelectItem>
                  {registry.data?.gateways.filter(target => invocationGateways.some(choice => choice.targetId === target.id)).map(target => <SelectItem value={target.id} key={target.id}>{target.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            {explicitGateway && <div className="invoke-select">
              <span>Gateway</span>
              <Select value={gatewaySelection.deploymentId} disabled={isInvoking || topology.isLoading || Boolean(topology.error)} onValueChange={value => setGatewaySelection(current => ({ ...current, deploymentId: value }))}>
                <SelectTrigger aria-label="Gateway"><SelectValue>{selectedGateway?.name ?? "选择 Gateway API"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {invocationGateways.filter(choice => choice.targetId === gatewaySelection.targetId).map(choice => <SelectItem key={choice.value} value={choice.deploymentId!} disabled={!choice.selectable} title={choice.reason ?? choice.endpoint ?? undefined}>{choice.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>}
            {topology.error && <div className="invoke-error" role="alert">{queryError(topology.error)}</div>}
            {explicitGateway && !topology.isLoading && !gatewayReady && <div className="invoke-error" role="alert">请选择可用的 Gateway API。</div>}
          </>}
          {imageMode && imageProfile && effectiveImageOptions && <ImageOptions value={effectiveImageOptions} onChange={setImageOptions} disabled={isInvoking} />}
          {imageMode && registry.data?.image_configuration_schema_version !== 4 && <div className="invoke-error" role="alert">后端尚未支持图像参数透传</div>}
          {mode === "chat" && <label className="invoke-select">
            <span>最大输出 Tokens</span>
            <Input type="number" aria-label="最大输出 Tokens" min={1} max={1_000_000} step={1} value={outputLimit} disabled={isInvoking} aria-invalid={!validOutputLimit} onChange={event => setOutputLimit(event.target.value)} />
          </label>}
          <div className="invoke-prompt">
            <span>{decisionMode ? "State" : "Prompt"}</span>
            {decisionMode && <ButtonGroup className="usage-metric-segment invoke-state-format" aria-label="State 格式">
              <button type="button" className={stateFormat === "text" ? "active" : ""} aria-pressed={stateFormat === "text"} disabled={isInvoking} onClick={() => selectStateFormat("text")}>Text</button>
              <button type="button" className={stateFormat === "json" ? "active" : ""} aria-pressed={stateFormat === "json"} disabled={isInvoking} onClick={() => selectStateFormat("json")}>JSON</button>
            </ButtonGroup>}
            <div className="invoke-composer">
              <textarea
                aria-label={decisionMode ? "State" : "Prompt"}
                value={prompt}
                disabled={isInvoking}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Enter" || (!event.metaKey && !event.ctrlKey) || event.nativeEvent.isComposing) return
                  event.preventDefault()
                  if (canInvoke) void invoke()
                }}
                rows={7}
              />
            </div>
          </div>
          {decisionMode && <label className="invoke-prompt"><span>Questions · JSON</span>
            <div className="invoke-composer"><textarea aria-label="Questions" value={questions} disabled={isInvoking}
              spellCheck={false} onChange={event => setQuestions(event.target.value)} rows={10} /></div>
          </label>}
          {invokeError && (
            <div className="invoke-error" role="alert">
              {invokeError}
            </div>
          )}
          <button
            type="button"
            className="primary-button"
            title={isInvoking ? "调用中..." : "发起调用 · ⌘↵"}
            disabled={!canInvoke}
            onClick={() => void invoke()}
          >
            {isInvoking ? <RefreshCw className="spin" size={15} /> : <Send size={15} />}
            {isInvoking ? "调用中..." : imageMode ? "生成图片" : "发起调用"}
          </button>
        </div>
        <details className="invoke-code-sample">
          <summary><Code2 size={14} aria-hidden="true" /><span>示例代码</span><ChevronRight size={14} aria-hidden="true" /></summary>
          {sampleError ? <div className="application-card-empty" role="alert">{sampleError}</div>
            : !sampleModelKey ? <div className="application-card-empty">{decisionMode ? "选择 Decision 模型" : imageMode ? "选择图片模型" : "所选模型不可用"}</div>
              : !sampleEndpoint ? <div className="application-card-empty">该 APIM 尚未提供可用的 API Base URL。</div>
                : !gatewayReady ? <div className="application-card-empty">请选择可用的 Gateway API。</div>
                  : <ClientSampleCode endpoint={sampleEndpoint} sampleForLanguage={language => sampleRequest ? clientSampleFor(language, sampleEndpoint, sampleModelKey, sampleOperation, sampleRequest) : ""} />}
        </details>
      </section>
      <section className="finops-panel invoke-result">
        {!imageMode && <PanelTitle title="调用结果" />}
        {imageMode ? <ImageGenerationResult result={imageResult} pending={isInvoking} available={registry.data?.image_generation_supported === true} /> : result ? (
          <div className="invoke-response">
            <dl className="invoke-result-facts">
              {"router_name" in result && result.router_name && <div>
                <dt>模型路由器</dt>
                <dd>{result.router_name} · v{result.router_version}</dd>
              </div>}
              <div>
                <dt>网关</dt>
                <dd>{result.native_gateway_id ?? result.gateway.toUpperCase()}</dd>
              </div>
              {result.gateway_profile_id && <div><dt>APIM 实例</dt><dd>{registry.data?.gateways.find(item => item.id === result.gateway_profile_id)?.name ?? result.gateway_profile_id}</dd></div>}
              <div>
                <dt>{"router_name" in result && result.router_name ? "最终模型" : "模型"}</dt>
                <dd>{"selected_model_name" in result ? result.selected_model_name ?? result.model : result.model}</dd>
              </div>
              <div>
                <dt>运行时</dt>
                <dd>{result.runtime}</dd>
              </div>
              <div>
                <dt>延迟</dt>
                <dd>{formatLatency(result.latency_ms)}</dd>
              </div>
              <div>
                <dt>Token{result.usage?.estimated ? "（估算）" : ""}</dt>
                <dd>
                  {totalTokens == null ? "—" : compact.format(totalTokens)}
                </dd>
              </div>
              <div>
                <dt>Cache Read</dt>
                <dd>{cacheReadTokens == null ? "—" : compact.format(cacheReadTokens)}</dd>
              </div>
              <div>
                <dt>Cache Write</dt>
                <dd>{result.usage == null ? "—" : compact.format(cacheWriteTokens)}</dd>
              </div>
              <div>
                <dt>估算成本</dt>
                <dd>
                  {result.estimated_cost == null
                    ? "未计价"
                    : `$${result.estimated_cost.toFixed(6)}`}
                </dd>
              </div>
            </dl>
            <dl className="invoke-result-ids">
              <div>
                <dt>Request ID</dt>
                <dd>{result.request_id}</dd>
              </div>
              <div>
                <dt>Correlation ID</dt>
                <dd>{result.correlation_id ?? "未知"}</dd>
              </div>
            </dl>
            <div className="invoke-result-content">
              <span>{"answers" in result ? "Answers" : "响应内容"}</span>
              {"answers" in result ? <pre>{JSON.stringify(result.answers, null, 2)}</pre> : result.content.trim()
                ? <pre>{result.content}</pre>
                : <div className="invoke-empty-content"><AlertTriangle size={16} /><div><b>模型未返回可见文本</b><span>{exhaustedOutputBudget ? `本次输出使用了 ${result.usage?.output_tokens ?? 0} Token，已达到请求上限；模型可能在生成可见回答前耗尽了输出预算。后续调用已提高上限。` : "模型返回了 Token 用量，但响应内容为空，请重试或缩短提示词。"}</span></div></div>}
            </div>
          </div>
        ) : (
          <EmptyState
            title="尚未调用"
            detail="选择业务元数据和模型后发起请求。"
          />
        )}
      </section>
    </div>
  );
}
