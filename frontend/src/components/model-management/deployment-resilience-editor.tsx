import {
  CheckCircle2,
  CircleAlert,
  ListChecks,
  LoaderCircle,
  RotateCcw,
  Send,
  ShieldCheck,
  Trash2,
} from "lucide-react"
import { useEffect, useRef, useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { dataSource } from "../../data-sources/apim/api"
import { ApiError } from "../../api/client"
import { finopsKeys, finopsQueries } from "../../data-sources/apim/queries"
import type {
  GatewayBackendPoolConfig,
  GatewayBackendPoolCandidate,
  GatewayBackendPoolMember,
  GatewayBackendPoolSummary,
  GatewayBackendPoolWrite,
  GatewayPublication,
  GatewayPublicationStatus,
  ManagedModel,
} from "../../data-sources/apim/types"
import { useAuth } from "../../providers/auth-provider"
import { ProviderBrandLogo, providerBrandFromIdentity, providerBrandLabel } from "../brand-logos"
import { FieldHelp } from "../model-management/field-help"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from "../ui/alert-dialog"
import { Checkbox } from "../ui/checkbox"
import { Button } from "../ui/button"
import { ButtonGroup } from "../ui/button-group"
import { Input } from "../ui/input"
import { previewNativeRouteDeployments } from "./apim-native-route-eligibility"
import {
  deploymentMode,
  membersForMode,
  poolDeploymentModelId,
  poolMemberIdentity,
  poolPublicationPayload,
  type DeploymentMode,
} from "./pool-session-affinity"

export { deploymentMode } from "./pool-session-affinity"
export type { DeploymentMode } from "./pool-session-affinity"

export type PublicationAction = "configure" | "remove"

function submissionFailure(err: unknown) {
  return err instanceof ApiError && err.detail ? err.detail : err instanceof Error ? err.message : String(err)
}

export type PendingBackendPoolSubmission = {
  modelId: string
  publication: GatewayPublication
  draft: GatewayBackendPoolWrite
  action?: PublicationAction
}

export const PENDING_SUBMISSIONS_STORAGE_KEY = "turnstile_apim_native_route_pending_submissions"

let cachedPendingSubmissions: Record<string, PendingBackendPoolSubmission> | null = null
const pendingSubmissionListeners = new Set<() => void>()

export function isSubmittingPublicationId(id: string | null | undefined): boolean {
  return Boolean(id && id.startsWith("submitting:"))
}

export function createOptimisticPoolPublication(
  model: ManagedModel,
  gatewayProfileId?: string | null,
): GatewayPublication {
  const now = new Date().toISOString()
  return {
    id: `submitting:${model.id}:${Date.now()}`,
    configuration_only: true,
    gateway_profile_id: gatewayProfileId ?? "",
    generation: 0,
    publication_kind: "route_reconcile",
    model_key: model.model_key,
    display_name: model.display_name,
    status: "queued",
    error_code: null,
    error_message: null,
    failure: null,
    authorization: null,
    retry_requires_credential: false,
    attempt_count: 0,
    created_by: "",
    created_at: now,
    started_at: null,
    completed_at: null,
    updated_at: now,
  }
}

export function readPendingBackendPoolSubmissions(): Record<string, PendingBackendPoolSubmission> {
  if (cachedPendingSubmissions) return cachedPendingSubmissions
  if (typeof sessionStorage !== "undefined") {
    try {
      const raw = sessionStorage.getItem(PENDING_SUBMISSIONS_STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw) as Record<string, PendingBackendPoolSubmission>
        if (parsed && typeof parsed === "object") {
          const restored: Record<string, PendingBackendPoolSubmission> = {}
          for (const [key, item] of Object.entries(parsed)) {
            if (item?.publication?.id && !isSubmittingPublicationId(item.publication.id)) {
              restored[key] = item
            }
          }
          cachedPendingSubmissions = restored
          return restored
        }
      }
    } catch {
      // Ignore storage read failures
    }
  }
  cachedPendingSubmissions = {}
  return cachedPendingSubmissions
}

function writePendingBackendPoolSubmissions(next: Record<string, PendingBackendPoolSubmission>) {
  cachedPendingSubmissions = next
  if (typeof sessionStorage !== "undefined") {
    try {
      if (Object.keys(next).length === 0) {
        sessionStorage.removeItem(PENDING_SUBMISSIONS_STORAGE_KEY)
      } else {
        sessionStorage.setItem(PENDING_SUBMISSIONS_STORAGE_KEY, JSON.stringify(next))
      }
    } catch {
      // Ignore storage write failures
    }
  }
  for (const listener of pendingSubmissionListeners) listener()
}

export function upsertPendingBackendPoolSubmission(submission: PendingBackendPoolSubmission) {
  const current = readPendingBackendPoolSubmissions()
  writePendingBackendPoolSubmissions({
    ...current,
    [submission.modelId]: {
      ...submission,
      action: submission.action ?? "configure",
    },
  })
}

export function removePendingBackendPoolSubmission(modelId: string) {
  const current = readPendingBackendPoolSubmissions()
  if (!(modelId in current)) return
  const next = { ...current }
  delete next[modelId]
  writePendingBackendPoolSubmissions(next)
}

export function subscribePendingBackendPoolSubmissions(listener: () => void) {
  pendingSubmissionListeners.add(listener)
  return () => {
    pendingSubmissionListeners.delete(listener)
  }
}

const DEFAULT_RATE_LIMIT: GatewayBackendPoolWrite["rate_limit"] = {
  max_attempts_per_request: 2,
  retry_interval_seconds: 1,
  first_fast_retry: true,
  backend_timeout_seconds: 120,
  circuit_breaker: {
    failure_count: 1,
    interval_seconds: 60,
    trip_duration_seconds: 60,
    accept_retry_after: true,
    status_code_ranges: [
      { minimum: 429, maximum: 429 },
      { minimum: 408, maximum: 408 },
      { minimum: 500, maximum: 599 },
    ],
    error_reasons: ["BackendConnectionFailure", "Timeout"],
  },
}

const TERMINAL_PUBLICATION_STATUSES = new Set<GatewayPublicationStatus>([
  "active",
  "failed",
  "awaiting_authorization",
  "rolled_back",
  "superseded",
])

const PUBLICATION_LABELS: Record<GatewayPublicationStatus, string> = {
  queued: "等待发布",
  validating: "正在创建 APIM 后端",
  provisioning: "正在同步路由数据",
  building_revision: "正在校验路由快照",
  verifying: "正在逐成员验证",
  awaiting_authorization: "等待提供方授权",
  promoting: "正在切换生效路由",
  active: "配置已生效",
  failed: "发布失败",
  superseded: "已被后续发布替代",
  rolling_back: "正在回滚",
  rolled_back: "已回滚",
}

type PublicationNotice = {
  status: GatewayPublicationStatus
  message: string
  publicationId?: string
}

function draftFromPool(
  pool: GatewayBackendPoolConfig | null,
  model: ManagedModel,
  models: ManagedModel[] = [],
): GatewayBackendPoolWrite {
  if (pool) {
    return {
      members: pool.members.map((member) => {
        const resolvedModelId = member.model_id ?? (
          models.length ? poolDeploymentModelId(member, model, models) : undefined
        )
        return {
          runtime_id: member.runtime_id,
          ...(resolvedModelId ? { model_id: resolvedModelId } : {}),
          priority: member.priority,
          weight: member.weight,
        }
      }),
      rate_limit: pool.rate_limit,
      session_affinity: pool.session_affinity === true,
    }
  }
  return {
    members: [{ runtime_id: model.runtime_id, model_id: model.id, priority: 0, weight: 1 }],
    rate_limit: structuredClone(DEFAULT_RATE_LIMIT),
    session_affinity: false,
  }
}

function draftSignature(value: GatewayBackendPoolWrite) {
  return JSON.stringify(value)
}

export function DeploymentResilienceEditor({
  model,
  initialDraft: providedInitialDraft,
  initialPublicationId,
  initialPublicationAction,
  onSubmitted,
  onActivated,
  onBusyChange,
  renderFrame,
}: {
  model: ManagedModel
  initialDraft?: GatewayBackendPoolWrite | null
  initialPublicationId?: string | null
  initialPublicationAction?: PublicationAction | null
  onSubmitted?: (submission: PendingBackendPoolSubmission) => void
  onActivated?: () => void
  onBusyChange?: (busy: boolean) => void
  renderFrame?: (editor: ReactNode, controls: ReactNode) => ReactNode
}) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canManage = user?.role === "owner"
  const [membersExpanded, setMembersExpanded] = useState(false)
  const registry = useQuery(finopsQueries.registry())
  const affinitySupported = registry.data?.backend_pool_session_affinity_supported === true
  const cachedPools = queryClient.getQueryData<GatewayBackendPoolSummary[]>(finopsKeys.modelBackendPools)
  const pool = useQuery({
    ...finopsQueries.modelBackendPool(model.id),
    initialData: () => cachedPools?.find(item => item.model_id === model.id)?.pool
      ?? (cachedPools ? null : undefined),
    initialDataUpdatedAt: () => queryClient.getQueryState(finopsKeys.modelBackendPools)?.dataUpdatedAt,
  })
  const candidates = useQuery({
    ...finopsQueries.modelBackendPoolCandidates(model.id),
    enabled: canManage && (!pool.data || membersExpanded),
  })
  const primaryRuntime = registry.data?.runtimes.find(
    (runtime) => runtime.id === model.runtime_id,
  )
  const memberModelId = (member: GatewayBackendPoolWrite["members"][number] | GatewayBackendPoolMember) =>
    poolDeploymentModelId(member, model, registry.data?.models ?? [])
  const primaryMember = pool.data?.members.find(member => memberModelId(member) === model.id)
  const primaryMemberId = primaryMember ? poolMemberIdentity(primaryMember) : model.id
  const initialDraft = providedInitialDraft
    ? structuredClone(providedInitialDraft)
    : draftFromPool(null, model)
  const baselineRef = useRef(initialDraft)
  const [baselineSignature, setBaselineSignature] = useState(() =>
    draftSignature(initialDraft),
  )
  const [draft, setDraft] = useState<GatewayBackendPoolWrite>(initialDraft)
  const [mode, setMode] = useState<DeploymentMode | null>(null)
  const [publicationId, setPublicationId] = useState<string | null>(initialPublicationId ?? null)
  const [publicationAction, setPublicationAction] = useState<PublicationAction | null>(
    initialPublicationAction ?? (initialPublicationId ? "configure" : null),
  )
  const [notice, setNotice] = useState<PublicationNotice | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)
  const publication = useQuery(finopsQueries.gatewayPublication(publicationId))

  useEffect(() => {
    if (!initialPublicationId) return
    setPublicationId(initialPublicationId)
    setPublicationAction(initialPublicationAction ?? "configure")
  }, [initialPublicationAction, initialPublicationId])

  useEffect(() => {
    if (!pool.isSuccess) return
    if (!pool.data && (initialPublicationAction === "remove" || publicationAction === "remove")) {
      return
    }
    const next = draftFromPool(pool.data, model, registry.data?.models ?? [])
    const previous = baselineRef.current
    baselineRef.current = next
    setBaselineSignature(draftSignature(next))
    if (providedInitialDraft) {
      const pendingDraft = structuredClone(providedInitialDraft)
      setMode(deploymentMode(pendingDraft.members, primaryMemberId))
      setDraft(pendingDraft)
      return
    }
    setMode(pool.data ? deploymentMode(next.members, primaryMemberId) : null)
    setDraft((current) => draftSignature(current) === draftSignature(previous)
      ? next
      : current)
  }, [initialPublicationAction, model, pool.data, pool.dataUpdatedAt, pool.isSuccess, primaryMemberId, providedInitialDraft, publicationAction, registry.data?.models])

  useEffect(() => {
    const status = publication.data?.status
    if (!publicationId || !status || !TERMINAL_PUBLICATION_STATUSES.has(status)) return
    if (status === "active") {
      if (publicationAction === "remove") {
        queryClient.setQueryData<GatewayBackendPoolSummary[]>(
          finopsKeys.modelBackendPools,
          (current) => current?.filter((item) => item.model_id !== model.id),
        )
        queryClient.setQueryData?.(finopsKeys.modelBackendPool(model.id), null)
        removePendingBackendPoolSubmission(model.id)
      } else {
        void queryClient.invalidateQueries({ queryKey: finopsKeys.modelBackendPool(model.id) })
      }
      setNotice({
        status,
        message: publicationAction === "remove"
          ? "APIM 后端池已移除，直接后端已恢复"
          : "APIM 部署韧性配置已生效",
      })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.modelBackendPools })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.modelBackendPoolCandidates(model.id) })
      void queryClient.invalidateQueries({ queryKey: finopsKeys.gatewayPublications })
      if (publicationAction === "configure") onActivated?.()
    } else if (status === "failed" || status === "rolled_back") {
      setNotice({
        status,
        message: publication.data?.error_message ?? "APIM 后端池发布失败",
        publicationId,
      })
    } else {
      setNotice({ status, message: PUBLICATION_LABELS[status] })
    }
    setPublicationId(null)
    setPublicationAction(null)
  }, [model.id, onActivated, publication.data, publicationAction, publicationId, queryClient])

  const save = useMutation({
    mutationFn: () => dataSource.saveModelBackendPool(
      model.id, poolPublicationPayload(draft, affinitySupported),
    ),
    onMutate: () => {
      const optimistic = createOptimisticPoolPublication(
        model,
        primaryRuntime?.gateway_profile_id,
      )
      const submittedDraft = structuredClone(draft)
      const submission: PendingBackendPoolSubmission = {
        modelId: model.id,
        publication: optimistic,
        draft: submittedDraft,
        action: "configure",
      }
      setNotice(null)
      setPublicationAction("configure")
      setPublicationId(optimistic.id)
      queryClient.setQueryData?.(
        finopsKeys.gatewayPublication(optimistic.id),
        optimistic,
      )
      upsertPendingBackendPoolSubmission(submission)
      onSubmitted?.(submission)
      return submission
    },
    onSuccess: (accepted, _variables, context) => {
      setNotice(null)
      setPublicationAction("configure")
      setPublicationId(accepted.publication.id)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(accepted.publication.id),
        accepted.publication,
      )
      const submission: PendingBackendPoolSubmission = {
        modelId: model.id,
        publication: accepted.publication,
        draft: context?.draft ?? structuredClone(draft),
        action: "configure",
      }
      upsertPendingBackendPoolSubmission(submission)
      onSubmitted?.(submission)
    },
    onError: (err, _variables, context) => {
      const base = context?.publication ?? createOptimisticPoolPublication(
        model,
        primaryRuntime?.gateway_profile_id,
      )
      const failed: GatewayPublication = {
        ...base,
        status: "failed",
        error_message: submissionFailure(err),
      }
      setPublicationId(null)
      setPublicationAction(null)
      setNotice({
        status: "failed",
        message: failed.error_message ?? "APIM 后端池发布失败",
      })
      queryClient.setQueryData?.(
        finopsKeys.gatewayPublication(failed.id),
        failed,
      )
      const submission: PendingBackendPoolSubmission = {
        modelId: model.id,
        publication: failed,
        draft: context?.draft ?? structuredClone(draft),
        action: "configure",
      }
      upsertPendingBackendPoolSubmission(submission)
      onSubmitted?.(submission)
    },
  })
  const remove = useMutation({
    mutationFn: () => dataSource.deleteModelBackendPool(model.id),
    onMutate: () => {
      const optimistic = createOptimisticPoolPublication(
        model,
        primaryRuntime?.gateway_profile_id,
      )
      const submittedDraft = structuredClone(draft)
      const submission: PendingBackendPoolSubmission = {
        modelId: model.id,
        publication: optimistic,
        draft: submittedDraft,
        action: "remove",
      }
      setRemoveOpen(false)
      setNotice(null)
      setPublicationAction("remove")
      setPublicationId(optimistic.id)
      queryClient.setQueryData?.(
        finopsKeys.gatewayPublication(optimistic.id),
        optimistic,
      )
      upsertPendingBackendPoolSubmission(submission)
      onSubmitted?.(submission)
      return submission
    },
    onSuccess: (accepted, _variables, context) => {
      setRemoveOpen(false)
      setNotice(null)
      setPublicationAction("remove")
      setPublicationId(accepted.publication.id)
      queryClient.setQueryData(
        finopsKeys.gatewayPublication(accepted.publication.id),
        accepted.publication,
      )
      const submission: PendingBackendPoolSubmission = {
        modelId: model.id,
        publication: accepted.publication,
        draft: context?.draft ?? structuredClone(draft),
        action: "remove",
      }
      upsertPendingBackendPoolSubmission(submission)
      onSubmitted?.(submission)
    },
    onError: (err, _variables, context) => {
      const base = context?.publication ?? createOptimisticPoolPublication(
        model,
        primaryRuntime?.gateway_profile_id,
      )
      const failed: GatewayPublication = {
        ...base,
        status: "failed",
        error_message: submissionFailure(err),
      }
      setPublicationId(null)
      setPublicationAction(null)
      setNotice({
        status: "failed",
        message: failed.error_message ?? "APIM 后端池发布失败",
      })
      queryClient.setQueryData?.(
        finopsKeys.gatewayPublication(failed.id),
        failed,
      )
      const submission: PendingBackendPoolSubmission = {
        modelId: model.id,
        publication: failed,
        draft: context?.draft ?? structuredClone(draft),
        action: "remove",
      }
      upsertPendingBackendPoolSubmission(submission)
      onSubmitted?.(submission)
    },
  })

  const publicationStatus = publication.data?.status
    ?? (isSubmittingPublicationId(publicationId) ? "queued" : undefined)
  const publishing = Boolean(
    publicationId
    && publicationStatus
    && !TERMINAL_PUBLICATION_STATUSES.has(publicationStatus),
  )
  const configuring = save.isPending || (publishing && publicationAction === "configure")
  const removing = remove.isPending || (publishing && publicationAction === "remove")
  const busy = configuring || removing
  useEffect(() => {
    onBusyChange?.(busy)
    return () => onBusyChange?.(false)
  }, [busy, onBusyChange])
  const dirty = draftSignature(draft) !== baselineSignature
  const validMemberCount = draft.members.length >= 2 && draft.members.length <= 30
  const selectedWeights = draft.members.map((member) => member.weight)
  const weightedIsDistinct = mode !== "weighted" || new Set(selectedWeights).size > 1
  const validDraft = validMemberCount && weightedIsDistinct
  const hasEffectivePool = Boolean(pool.data || providedInitialDraft)
  const hasPendingPool = !hasEffectivePool && validMemberCount
  const visiblePublicationStatus = publicationStatus ?? notice?.status
  const visiblePublicationMessage = publicationStatus
    ? PUBLICATION_LABELS[publicationStatus]
    : notice?.message
  const publicationHasError = visiblePublicationStatus === "failed"
    || visiblePublicationStatus === "rolled_back"
  const selectedIds = new Set(draft.members.map(memberModelId))
  const effectiveMemberIds = new Set(pool.data?.members.map(memberModelId))
  const verifiedIds = new Set(candidates.data?.map(candidate => candidate.model_id))
  const showCandidates = !hasEffectivePool || membersExpanded
  const previewCandidates: GatewayBackendPoolCandidate[] = registry.data && showCandidates
    ? previewNativeRouteDeployments(model, registry.data).map(candidate => {
      const runtime = registry.data!.runtimes.find(item => item.id === candidate.runtime_id)!
      return {
        model_id: candidate.id,
        runtime_id: candidate.runtime_id,
        runtime_name: runtime.name,
        deployment_name: candidate.upstream_model_id ?? candidate.model_key,
        project_endpoint: String(runtime.config.project_endpoint ?? "") || null,
        model_name: candidate.display_name,
        model_version: null,
        model_publisher: candidate.provider_name,
      }
    }) : []
  const deployments = new Map<string, GatewayBackendPoolCandidate>()
  for (const member of [...pool.data?.members ?? [], ...draft.members]) {
    const modelId = memberModelId(member)
    const saved = pool.data?.members.find(item => memberModelId(item) === modelId)
    const registered = registry.data?.models.find(item => item.id === modelId)
    const runtime = registry.data?.runtimes.find(item => item.id === member.runtime_id)
    deployments.set(modelId, {
      model_id: modelId,
      runtime_id: member.runtime_id,
      runtime_name: saved?.runtime_name ?? runtime?.name ?? member.runtime_id,
      deployment_name: saved?.upstream_model_id ?? registered?.upstream_model_id ?? model.upstream_model_id ?? model.model_key,
      project_endpoint: saved?.project_endpoint ?? (String(runtime?.config.project_endpoint ?? "") || null),
      model_name: model.display_name,
      model_version: null,
      model_publisher: model.provider_name,
    })
  }
  for (const candidate of candidates.data ?? previewCandidates) {
    if (showCandidates || selectedIds.has(candidate.model_id) || effectiveMemberIds.has(candidate.model_id)) {
      deployments.set(candidate.model_id, candidate)
    }
  }
  const visibleDeployments = [...deployments.values()]
    .sort((left, right) => {
      if (left.model_id === model.id) return -1
      if (right.model_id === model.id) return 1
      const selectedDifference = Number(selectedIds.has(right.model_id)) - Number(selectedIds.has(left.model_id))
      return selectedDifference || left.deployment_name.localeCompare(right.deployment_name)
    })
  const groups = new Map<string, { name: string; deployments: GatewayBackendPoolCandidate[] }>()
  for (const deployment of visibleDeployments) {
    const group = groups.get(deployment.runtime_id)
    if (group) group.deployments.push(deployment)
    else groups.set(deployment.runtime_id, { name: deployment.runtime_name, deployments: [deployment] })
  }
  const toggleDeployment = (deployment: GatewayBackendPoolCandidate, selected: boolean) => {
    const nextMode = mode === "balanced" || mode === "weighted" ? mode : "failover"
    if (selected && mode === null) setMode(nextMode)
    setDraft((current) => ({
      ...current,
      members: selected
        ? [
            ...current.members,
            {
              runtime_id: deployment.runtime_id,
              model_id: deployment.model_id,
              priority: nextMode !== "failover" || deployment.model_id === model.id ? 0 : 1,
              weight: 1,
            },
          ]
        : current.members.filter((member) => memberModelId(member) !== deployment.model_id),
    }))
  }
  const changeMode = (nextMode: Exclude<DeploymentMode, "custom">) => {
    setMode(nextMode)
    setDraft((current) => ({
      ...current,
      members: membersForMode(current.members, nextMode, primaryMemberId),
    }))
  }
  const changeWeight = (modelId: string, weight: number) => {
    setDraft((current) => ({
      ...current,
      members: current.members.map((member) => memberModelId(member) === modelId
        ? { ...member, weight: Math.min(100, Math.max(1, weight || 1)) }
        : member),
    }))
  }
  const totalWeight = draft.members.reduce((sum, member) => sum + member.weight, 0)

  // Save and remove failures are already shown once in the publication notice.
  const error = pool.error ?? publication.error ?? candidates.error
  const selectableCandidates = candidates.data ?? previewCandidates
  const selectableIds = candidates.isPending
    ? new Set(previewCandidates.map(candidate => candidate.model_id))
    : verifiedIds
  const canConfigure = canManage && pool.isSuccess && (hasEffectivePool || selectableCandidates.length >= 2)
    && (!showCandidates || (candidates.isPending || candidates.isSuccess)
      && [...selectedIds].every(id => selectableIds.has(id)))
  const isRemoveFlow = initialPublicationAction === "remove"
    || publicationAction === "remove"
    || notice?.message === "APIM 后端池已移除，直接后端已恢复"
  const awaitingPoolRefresh = !pool.data && Boolean(providedInitialDraft) && visiblePublicationStatus === "active"
  const showChangeActions = canManage
    && !isRemoveFlow
    && (dirty || configuring || !hasEffectivePool)
    && !(Boolean(renderFrame) && hasEffectivePool && (publishing || awaitingPoolRefresh))
  const changeActions = showChangeActions ? <span className="apim-native-route-change-actions">
    {Boolean(pool.data) && dirty && !configuring && <span className="apim-native-route-dirty">未保存</span>}
    {Boolean(pool.data) && dirty && !busy && <Button type="button" variant="ghost" size="icon-sm" title="取消更改" aria-label="取消更改" onClick={() => {
      setDraft(structuredClone(baselineRef.current))
      setMode(pool.data ? deploymentMode(baselineRef.current.members, primaryMemberId) : null)
    }}><RotateCcw size={13} /></Button>}
    <Button className="router-action-primary" type="button" size="sm" disabled={busy || !canConfigure || !validDraft} onClick={() => save.mutate()}>{configuring ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{configuring ? "正在发布" : hasEffectivePool ? "发布到 APIM" : "创建"}</Button>
  </span> : null
  const disableAction = canManage && pool.data
    ? <Button className="apim-native-route-disable" type="button" variant="destructive" size="sm" disabled={busy} onClick={() => setRemoveOpen(true)}><Trash2 size={13} />删除 Pool</Button>
    : null
  const externalControls = pool.isLoading || registry.isLoading || !primaryRuntime || primaryRuntime.config.control_plane_managed !== true ? null : <>
    {publishing && publicationStatus && <span className="apim-native-route-status" data-pending>{PUBLICATION_LABELS[publicationStatus]}</span>}
    {!publishing && (Boolean(pool.data) || awaitingPoolRefresh) && <span className="apim-native-route-status" data-active>{"已生效"}</span>}
    {changeActions}
    {disableAction}
  </>
  const editor = <section className="router-deployment-resilience">
    {registry.isLoading ? <div className="router-resilience-state"><LoaderCircle className="spin" size={14} />加载 APIM 后端池</div> : !primaryRuntime || primaryRuntime.config.control_plane_managed !== true ? <div className="router-resilience-state warning"><CircleAlert size={14} />此模型不支持 APIM 后端池</div> : <>
      <div className="router-resilience-toolbar router-resilience-members-heading">
        <span><b>{hasEffectivePool ? "池成员" : "选择部署"}</b><small>{draft.members.length} <span>已选</span>{!validMemberCount && <> · <span>至少选择两个部署</span></>}</small></span>
        <div className="router-resilience-member-actions">
          {hasEffectivePool && canManage && <Button type="button" variant="outline" size="sm" disabled={busy} aria-expanded={membersExpanded} onClick={() => setMembersExpanded(!membersExpanded)}><ListChecks size={14} />{membersExpanded ? "收起候选" : "管理成员"}</Button>}
          {!renderFrame && hasEffectivePool && <i data-active>{"已生效"}</i>}
        </div>
      </div>
      <div className="router-resilience-runtime-list" role="list" data-pool={hasEffectivePool} aria-label={`${model.display_name} APIM Backend Pool Runtime`}>
        {showCandidates && candidates.isPending && !previewCandidates.length && <div className="router-resilience-state" role="status"><LoaderCircle className="spin" size={14} />加载部署列表</div>}
        {[...groups.entries()].map(([runtimeId, group]) => <div className="router-resilience-project" key={runtimeId}>
          {group.deployments.map((deployment) => {
          const selected = selectedIds.has(deployment.model_id)
          const member = draft.members.find((candidate) => memberModelId(candidate) === deployment.model_id)
          const primary = deployment.model_id === model.id
          const unavailable = showCandidates && candidates.isSuccess && !verifiedIds.has(deployment.model_id)
          const shownSelected = selected
          const membershipState = selected
            ? effectiveMemberIds.has(deployment.model_id) ? "已生效" : "待加入"
            : effectiveMemberIds.has(deployment.model_id) ? "待移除" : unavailable ? "不可用" : "可选副本"
          const role = primary ? hasEffectivePool ? "主部署" : "必选" : selected ? mode === "failover" ? "备用部署" : "成员" : "可选副本"
          const trafficShare = member && totalWeight > 0
            ? Math.round(member.weight / totalWeight * 100)
            : 0
          const connectionBrand = providerBrandFromIdentity(deployment.runtime_name)
          const providerLabel = providerBrandLabel(connectionBrand)
          const connectionName = (deployment.runtime_name.startsWith(`${providerLabel} `)
            ? deployment.runtime_name.slice(providerLabel.length + 1)
            : deployment.runtime_name).replace(/\s+via APIM$/i, "")
          return <div className="router-resilience-runtime" role="listitem" data-selected={shownSelected} data-unavailable={unavailable} key={deployment.model_id}>
            <label className="router-resilience-member-heading"><Checkbox checked={shownSelected} title={primary ? "主部署（必选）" : deployment.deployment_name} disabled={!canManage || busy || primary || (!selected && (!candidates.isPending && !verifiedIds.has(deployment.model_id) || draft.members.length >= 30))} onCheckedChange={(checked) => toggleDeployment(deployment, checked === true)} /><span><b data-no-localize title={deployment.deployment_name}>{deployment.deployment_name}</b>{hasEffectivePool && <small>{membershipState}</small>}</span></label>
            <span className="router-resilience-member-role" data-primary={primary} data-selected={selected}>{role}</span>
            <span className="router-resilience-member-connection" data-no-localize title={deployment.runtime_name}><ProviderBrandLogo brand={connectionBrand} size={14} /><span>{connectionName}</span></span>
            {(hasEffectivePool || hasPendingPool && member) && <div className="router-resilience-member-meta">
              <span className="router-resilience-member-priority"><small>APIM</small><b>{member ? `P${member.priority + 1}` : "--"}</b></span>
              {mode === "weighted" && member ? <label className="router-resilience-member-weight"><small>权重</small><Input aria-label={`${deployment.runtime_name} ${deployment.deployment_name} 权重`} type="number" min="1" max="100" value={member.weight} disabled={!canManage || busy} onChange={(event) => changeWeight(deployment.model_id, Number(event.target.value))} /></label> : <span className="router-resilience-member-weight-readonly"><small>权重</small><b>{member?.weight ?? "--"}</b></span>}
              <span className="router-resilience-member-traffic"><small>流量占比</small><b>{member && mode !== "failover" ? `${trafficShare}%` : "--"}</b></span>
            </div>}
          </div>
        })}</div>)}
      </div>
      {(hasEffectivePool || hasPendingPool) && <>
      <div className="router-resilience-toolbar">
        <span><b>部署模式</b><small>选择同一模型的物理 Deployment 如何接收流量。</small></span>
      </div>
      <ButtonGroup className="usage-metric-segment router-deployment-mode" aria-label="切换 APIM 部署模式">
        <button type="button" className={validMemberCount && mode === "failover" ? "active" : ""} aria-pressed={validMemberCount && mode === "failover"} onClick={() => changeMode("failover")} disabled={!canManage || busy || !validMemberCount}><span className="router-mode-label-full">主备切换</span><span className="router-mode-label-short">主备</span></button>
        <button type="button" className={validMemberCount && mode === "balanced" ? "active" : ""} aria-pressed={validMemberCount && mode === "balanced"} onClick={() => changeMode("balanced")} disabled={!canManage || busy || !validMemberCount}><span className="router-mode-label-full">均衡分流</span><span className="router-mode-label-short">均衡</span></button>
        <button type="button" className={validMemberCount && mode === "weighted" ? "active" : ""} aria-pressed={validMemberCount && mode === "weighted"} onClick={() => changeMode("weighted")} disabled={!canManage || busy || !validMemberCount}><span className="router-mode-label-full">加权分流</span><span className="router-mode-label-short">加权</span></button>
      </ButtonGroup>
      {mode === "custom" && <div className="router-resilience-state warning"><CircleAlert size={14} />历史自定义配置只读</div>}
      {mode === "weighted" && !weightedIsDistinct && <div className="router-resilience-state warning"><CircleAlert size={14} />请设置至少两个不同权重；相同权重请使用均衡分流。</div>}
      <div className="router-resilience-affinity">
        <div className="router-resilience-affinity-control">
          <label><Checkbox checked={draft.session_affinity === true} disabled={!canManage || busy || !affinitySupported || !validMemberCount} onCheckedChange={(checked) => setDraft((current) => ({ ...current, session_affinity: checked === true }))} /><span>会话黏着</span></label>
          <FieldHelp>客户端须按对话保留 APIM Cookie。故障时可切换部署；调整池成员或配置可能重新分配会话。不会锁定 Azure 实际处理 Region。</FieldHelp>
          <span className="router-resilience-affinity-state">{pool.data?.session_affinity ? draft.session_affinity ? "黏着已生效" : "黏着待关闭" : draft.session_affinity ? "黏着待发布" : "黏着已关闭"}</span>
        </div>
        {!affinitySupported && <div className="router-resilience-state warning"><CircleAlert size={14} />当前后端未发布会话黏着能力。</div>}
        {draft.session_affinity && <dl><div><dt>绑定范围</dt><dd>Pool 后端端点</dd></div><div><dt>会话传递</dt><dd>Cookie</dd></div><div><dt>故障行为</dt><dd>允许故障切换</dd></div></dl>}
      </div>

      <details className="router-resilience-policy" data-active={hasEffectivePool} data-pending={hasPendingPool}>
        <summary><ShieldCheck size={14} />故障处理策略</summary>
        <dl><div><dt>故障信号</dt><dd>429 · 408 · 5xx · Connection · Timeout</dd></div><div><dt>熔断时间</dt><dd>{draft.rate_limit.circuit_breaker.trip_duration_seconds}s</dd></div><div><dt>最大尝试</dt><dd>{draft.rate_limit.max_attempts_per_request}</dd></div><div><dt>重试间隔</dt><dd>{draft.rate_limit.retry_interval_seconds}s</dd></div><div><dt>首次快速重试</dt><dd>{draft.rate_limit.first_fast_retry ? "启用" : "关闭"}</dd></div><div><dt>后端超时</dt><dd>{draft.rate_limit.backend_timeout_seconds}s</dd></div><div><dt>后端 Retry-After</dt><dd>{draft.rate_limit.circuit_breaker.accept_retry_after ? "采用" : "忽略"}</dd></div></dl>
      </details>
      </>}

      {!canManage && <div className="router-resilience-state"><ShieldCheck size={14} />只读</div>}
      {visiblePublicationStatus && visiblePublicationMessage && <div className="router-resilience-state" data-status={visiblePublicationStatus}>{publishing ? <LoaderCircle className="spin" size={14} /> : publicationHasError || visiblePublicationStatus === "awaiting_authorization" ? <CircleAlert size={14} /> : <CheckCircle2 size={14} />}<span>{visiblePublicationMessage}</span>{!publicationStatus && notice?.publicationId && <span className="router-resilience-task">任务 ID <code data-no-localize>{notice.publicationId}</code></span>}</div>}
      {error && <div className="router-resilience-state error"><CircleAlert size={14} /><span data-no-localize>{String(error)}</span></div>}
      {!renderFrame && (disableAction || changeActions) && <footer>{disableAction}{changeActions}</footer>}
      {removeOpen && <AlertDialog open={removeOpen} onOpenChange={(open) => { if (!busy) setRemoveOpen(open) }}>
        <AlertDialogContent className="model-delete-dialog router-confirm-dialog">
          <div className="model-delete-body">
            <AlertDialogTitle>删除 APIM 后端池？</AlertDialogTitle>
            <AlertDialogDescription>恢复单一后端，并影响该模型的所有调用。</AlertDialogDescription>
          </div>
          <AlertDialogFooter className="model-delete-footer">
            <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
            <AlertDialogAction variant="destructive" disabled={busy} onClick={() => remove.mutate()}>{remove.isPending ? "正在提交" : "删除"}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>}
    </>}
  </section>
  return renderFrame ? renderFrame(editor, externalControls) : editor
}