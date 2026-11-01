import { useState } from "react"
import { useMutation } from "@tanstack/react-query"
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  KeyRound,
  LoaderCircle,
  RotateCw,
} from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import type {
  GatewayApplicationDetail,
  GatewayApplicationSubscription,
  GatewayApplicationSubscriptionKeyKind,
} from "../../data-sources/apim/types"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from "../ui/alert-dialog"
import { Button } from "../ui/button"

type KeyTarget = {
  subscription: GatewayApplicationSubscription
  keyKind: GatewayApplicationSubscriptionKeyKind
}

const keyLabels = {
  primary: "Primary Key",
  secondary: "Secondary Key",
} as const

async function copySecret(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    return true
  } catch {
    return false
  }
}

export function ApplicationSubscriptionCard({
  application,
  canManage,
}: {
  application: GatewayApplicationDetail
  canManage: boolean
}) {
  const [rotateTarget, setRotateTarget] = useState<KeyTarget | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [clipboardError, setClipboardError] = useState<string | null>(null)
  const clearStatus = () => {
    setNotice(null)
    setClipboardError(null)
  }
  const copyMutation = useMutation({
    mutationFn: async (target: KeyTarget) => {
      const secret = await dataSource.revealGatewayApplicationSubscriptionKey(
        application.id,
        target.subscription.id,
        target.keyKind,
      )
      return { target, copied: await copySecret(secret.value) }
    },
    onMutate: clearStatus,
    onSuccess: ({ target, copied }) => {
      if (copied) setNotice(`${keyLabels[target.keyKind]} 已复制`)
      else setClipboardError("浏览器未允许访问剪贴板，请重试。")
    },
  })
  const rotateMutation = useMutation({
    mutationFn: async (target: KeyTarget) => {
      await dataSource.rotateGatewayApplicationSubscriptionKey(
        application.id,
        target.subscription.id,
        target.keyKind,
        target.subscription.apim_subscription_id,
      )
      try {
        const secret = await dataSource.revealGatewayApplicationSubscriptionKey(
          application.id,
          target.subscription.id,
          target.keyKind,
        )
        return { target, copied: await copySecret(secret.value) }
      } catch {
        return { target, copied: false }
      }
    },
    onMutate: clearStatus,
    onSuccess: ({ target, copied }) => {
      setRotateTarget(null)
      if (copied) setNotice(`${keyLabels[target.keyKind]} 已轮换并复制`)
      else setClipboardError(`${keyLabels[target.keyKind]} 已轮换；请点击复制重新获取。`)
    },
  })
  const busy = copyMutation.isPending || rotateMutation.isPending
  const managementReason = application.key_management_unavailable_reason
    ?? "密钥管理尚未部署"
  const actionDisabled = (
    subscription: GatewayApplicationSubscription,
  ) => busy || !application.key_management_available || subscription.state === "cancelled"
  const actionTitle = application.key_management_available
    ? undefined
    : managementReason
  const operationError = copyMutation.error ?? rotateMutation.error

  return <>
    <section className="application-card application-subscription-card">
      <header className="application-card-head">
        <div><KeyRound size={14} /><h2>APIM 订阅</h2></div>
        <span>{application.subscriptions.length}</span>
      </header>
      <div className="application-subscription-list">
        {application.subscriptions.map((subscription) => {
          const showSummary = application.subscriptions.length > 1
            || subscription.state !== "active"
            || !subscription.scope_exists
          return <div className="application-subscription-item" key={subscription.id}>
          {showSummary && <div className="application-subscription-summary">
            <span className="application-subscription-icon"><KeyRound size={13} /></span>
            <span><b title={subscription.apim_subscription_id} data-no-localize>{subscription.apim_subscription_id}</b><small><span>{subscription.scope_type === "product" ? "产品" : subscription.scope_type === "api" ? "API" : "服务"}</span><i>·</i><code title={subscription.scope_id} data-no-localize>{subscription.scope_id}</code></small></span>
            <em className={`${subscription.state} ${subscription.scope_exists ? "" : "stale"}`}>{subscription.scope_exists ? subscription.state === "active" ? "活动" : subscription.state === "suspended" ? "暂停" : "取消" : "失联"}</em>
          </div>}
          {canManage && !application.system_managed && <div className="application-subscription-key-list">
            {(["primary", "secondary"] as const).map((keyKind) => {
              const target = { subscription, keyKind }
              const copying = copyMutation.isPending
                && copyMutation.variables?.subscription.id === subscription.id
                && copyMutation.variables.keyKind === keyKind
              const rotating = rotateMutation.isPending
                && rotateMutation.variables?.subscription.id === subscription.id
                && rotateMutation.variables.keyKind === keyKind
              return <div className="application-subscription-key-row" key={keyKind}>
                <span><b>{keyLabels[keyKind]}</b><code aria-hidden="true">••••••••••••</code></span>
                <div>
                  <Button type="button" variant="ghost" size="icon-sm" disabled={actionDisabled(subscription)} title={actionTitle ?? `复制 ${keyLabels[keyKind]}`} aria-label={`复制 ${keyLabels[keyKind]}`} onClick={() => copyMutation.mutate(target)}>{copying ? <LoaderCircle className="spin" size={14} /> : <Copy size={14} />}</Button>
                  <Button type="button" variant="ghost" size="icon-sm" disabled={actionDisabled(subscription)} title={actionTitle ?? `轮换 ${keyLabels[keyKind]}`} aria-label={`轮换 ${keyLabels[keyKind]}`} onClick={() => { clearStatus(); setRotateTarget(target) }}>{rotating ? <LoaderCircle className="spin" size={14} /> : <RotateCw size={14} />}</Button>
                </div>
              </div>
            })}
            {!application.key_management_available && <small className="application-key-unavailable"><AlertTriangle size={12} />{managementReason}</small>}
          </div>}
        </div>})}
      </div>
      {notice && <div className="application-key-notice" role="status"><CheckCircle2 size={13} />{notice}</div>}
      {(clipboardError || operationError) && <div className="application-key-error" role="alert"><AlertTriangle size={13} />{clipboardError ?? String(operationError)}</div>}
    </section>

    <AlertDialog open={Boolean(rotateTarget)} onOpenChange={(open) => { if (!open && !rotateMutation.isPending) setRotateTarget(null) }}>
      <AlertDialogContent className="application-key-rotate-dialog" data-tone="destructive">
        <div className="application-key-rotate-body">
          <AlertDialogTitle>{rotateTarget ? `轮换 ${keyLabels[rotateTarget.keyKind]}？` : "轮换订阅密钥？"}</AlertDialogTitle>
          <AlertDialogDescription>{rotateTarget?.keyKind === "primary" ? "当前 Primary Key 将在网关同步后失效，在此之前可能仍短暂可用。请先让调用方切换到 Secondary Key。" : "当前 Secondary Key 将在网关同步后失效，在此之前可能仍短暂可用。请确认没有调用方仍在使用它。"}</AlertDialogDescription>
          {rotateTarget && <code data-no-localize>{rotateTarget.subscription.apim_subscription_id}</code>}
          {rotateMutation.error && <div className="application-key-rotate-error" role="alert"><AlertTriangle size={13} />{String(rotateMutation.error)}</div>}
        </div>
        <AlertDialogFooter className="application-key-rotate-footer">
          <AlertDialogCancel disabled={rotateMutation.isPending}>取消</AlertDialogCancel>
          <AlertDialogAction variant="destructive" disabled={rotateMutation.isPending} onClick={(event) => { event.preventDefault(); if (rotateTarget) rotateMutation.mutate(rotateTarget) }}>{rotateMutation.isPending ? <><LoaderCircle className="spin" size={13} />正在轮换</> : <><RotateCw size={13} />确认轮换</>}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </>
}