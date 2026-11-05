// Pure helper tests only; these fixtures are not browser or cloud validation evidence.
import assert from 'node:assert/strict'
import test from 'node:test'
import { applicationCreateError, applicationOperationIdFromUrl, applicationOperationTerminal, applicationProvisionBlocker, applicationProvisionStage, applicationRetryDetail, applicationSubscriptionIdFromName, applicationTagsError, provisionedApplicationId } from '../../../frontend/src/components/applications/application-create-form.ts'

const gateway = { id: 'gateway-a', enabled: true, implementation: 'apim' }
const value = { display_name: 'Invoice Service', subscription_id: 'invoice-service', application_type: 'service', description: null }
const applicationId = '00000000-0000-4000-8000-000000000001'
test('creation cannot bypass a disabled, direct or missing gateway', () => {
  assert.ok(applicationCreateError(undefined, value, []))
  assert.ok(applicationCreateError({ ...gateway, enabled: false }, value, []))
  assert.ok(applicationCreateError({ ...gateway, implementation: 'direct' }, value, []))
  assert.equal(applicationCreateError(gateway, value, []), null)
})
test('ID and duplicate validation is gateway scoped and includes reserved names', () => {
  for (const subscription_id of ['master', 'turnstile-dashboard', 'turnstile-publisher-probe', 'bad id', '', '../escape']) assert.ok(applicationCreateError(gateway, { ...value, subscription_id }, []))
  assert.ok(applicationCreateError(gateway, value, [{ gateway_profile_id: gateway.id, slug: value.subscription_id }]))
  assert.equal(applicationCreateError(gateway, value, [{ gateway_profile_id: 'gateway-b', slug: value.subscription_id }]), null)
  assert.ok(applicationCreateError(gateway, { ...value, display_name: ' ' }, []))
  assert.ok(applicationCreateError(gateway, { ...value, description: 'x'.repeat(1001) }, []))
})
test('name suggestion is deterministic and never invents a server identity', () => {
  assert.equal(applicationSubscriptionIdFromName('Invoice Service'), 'invoice-service')
  assert.equal(applicationSubscriptionIdFromName('Café'), 'cafe')
  assert.equal(applicationSubscriptionIdFromName('发票助手'), '')
  assert.equal(applicationSubscriptionIdFromName('a'.repeat(160)).length, 127)
})
test('only an explicit backend capability enables submission', () => {
  assert.ok(applicationProvisionBlocker(undefined))
  assert.ok(applicationProvisionBlocker({}))
  assert.ok(applicationProvisionBlocker({ provisioning_available: false, provisioning_unavailable_reason: 'Application subscription provisioning is not deployed.' }))
  assert.ok(applicationProvisionBlocker({ provisioning_available: true }))
  assert.equal(applicationProvisionBlocker({ provisioning_available: true, provisioning_options_supported: true }), null)
})
test('creation validates Token and exact USD budgets without changing TPM units', () => {
  assert.equal(applicationCreateError(gateway, { ...value, budget_mode: 'tokens', token_limit: 250000, tokens_per_minute: 24000 }, []), null)
  assert.equal(applicationCreateError(gateway, { ...value, budget_mode: 'usd', token_limit: null, usd_limit: '12.50', tokens_per_minute: 24000 }, []), null)
  for (const budget of [{ budget_mode: 'usd' }, { budget_mode: 'usd', usd_limit: '0' }, { budget_mode: 'usd', usd_limit: '1.001' }, { budget_mode: 'usd', usd_limit: '12.50', token_limit: 1 }, { token_limit: 1.5 }, { token_limit: 0 }, { tokens_per_minute: 0 }, { tokens_per_minute: 2147483648 }]) assert.ok(applicationCreateError(gateway, { ...value, ...budget }, []))
})
test('tags retain key-value identifiers and reject blank, duplicate or oversized keys', () => {
  assert.equal(applicationTagsError([]), null)
  assert.equal(applicationTagsError([{ key: 'team', value: 'finance' }]), null)
  assert.equal(applicationCreateError(gateway, { ...value, tags: { team: 'finance' } }, []), null)
  assert.ok(applicationTagsError([{ key: ' ', value: 'finance' }]))
  assert.ok(applicationTagsError([{ key: 'team', value: 'finance' }, { key: ' team ', value: 'other' }]))
  for (const tags of [{ '': 'finance' }, { ['x'.repeat(129)]: '' }, { team: 'x'.repeat(257) }, Object.fromEntries(Array.from({ length: 33 }, (_, index) => ['tag' + index, 'value']))]) assert.ok(applicationCreateError(gateway, { ...value, tags }, []))
})
test('queued and materialized operations are not reported as ready', () => {
  for (const status of ['queued', 'validating_dependencies', 'promoting', 'verifying_readback']) {
    const operation = { status, operation_kind: 'application_provision', checkpoint: { application_id: applicationId } }
    assert.equal(provisionedApplicationId(operation), null)
    assert.equal(applicationOperationTerminal(operation), false)
  }
  assert.equal(provisionedApplicationId({ status: 'succeeded', operation_kind: 'application_provision', checkpoint: { application_id: applicationId } }), applicationId)
  assert.equal(provisionedApplicationId({ status: 'failed', operation_kind: 'application_provision', checkpoint: { application_id: applicationId } }), null)
  assert.equal(applicationProvisionStage({ status: 'queued', worker_available: false }), '创建未启动')
})
test('refresh recovery accepts only operation UUIDs, never arbitrary or secret values', () => {
  assert.equal(applicationOperationIdFromUrl('https://unit.test/?applicationOperation=' + applicationId), applicationId)
  assert.equal(applicationOperationIdFromUrl('https://unit.test/?applicationOperation=invalid'), null)
  assert.equal(applicationOperationIdFromUrl('https://unit.test/'), null)
})
test('retry details suppress only a parsed subscription readiness diagnostic', () => {
  const prefix = 'Application data-plane authentication: '
  const waiting = { status: 'verifying_readback', error_message: prefix + JSON.stringify({ category: 'subscription_not_ready', status_code: 401, correlation_id: null }) }
  assert.equal(applicationRetryDetail(undefined), null)
  assert.equal(applicationRetryDetail({ status: waiting.status }), null)
  assert.equal(applicationRetryDetail({ ...waiting, error_message: null }), null)
  assert.equal(applicationRetryDetail({ ...waiting, error_message: '' }), null)
  assert.equal(applicationRetryDetail(waiting), null)
  for (const message of [
    prefix + JSON.stringify({ category: 'transport_error', status_code: null, correlation_id: null }),
    prefix + JSON.stringify({ category: 'transport_error', status_code: 401 }),
    '{"category":"subscription_not_ready","status_code":401}',
    'APIM permissions failed while checking subscription_not_ready',
    prefix + '{"category":"subscription_not_ready"',
    prefix + 'null',
    prefix + 'true',
    prefix + '401',
    prefix + '"subscription_not_ready"',
    prefix + '[]',
    prefix + '[{"category":"subscription_not_ready","status_code":401}]',
    prefix + '{}',
  ]) assert.equal(applicationRetryDetail({ ...waiting, error_message: message }), message)
  for (const status of ['failed', 'restored', 'succeeded']) {
    for (const error_message of [waiting.error_message, prefix + '{"category":"subscription_not_ready","status_code":503}', 'APIM permissions failed']) {
      assert.equal(applicationRetryDetail({ ...waiting, status, error_message }), null)
    }
  }
})
test('retry details preserve non-401 subscription readiness diagnostics verbatim', () => {
  const prefix = 'Application data-plane authentication: '
  for (const status_code of [503, 302, 404, 408, 429]) {
    const message = prefix + '{ "category": "subscription_not_ready", "status_code": ' + status_code + ', "correlation_id": null }\n'
    assert.equal(applicationRetryDetail({ status: 'verifying_readback', error_message: message }), message)
  }
})
test('retry details preserve missing and nonnumeric subscription readiness statuses verbatim', () => {
  const prefix = 'Application data-plane authentication: '
  for (const diagnostic of [
    '{"category":"subscription_not_ready"}',
    '{"category":"subscription_not_ready","status_code":null}',
    '{"category":"subscription_not_ready","status_code":"401"}',
    '{"category":"subscription_not_ready","status_code":"invalid"}',
    '{"category":"subscription_not_ready","status_code":true}',
    '{"category":"subscription_not_ready","status_code":false}',
    '{"category":"subscription_not_ready","status_code":[]}',
    '{"category":"subscription_not_ready","status_code":[401]}',
    '{"category":"subscription_not_ready","status_code":{}}',
  ]) {
    const message = prefix + diagnostic
    assert.equal(applicationRetryDetail({ status: 'verifying_readback', error_message: message }), message)
  }
})