// Isolated Router definition normalization tests. No browser, API or database.
import assert from "node:assert/strict"
import test from "node:test"

import {
  copyName,
  createRouterEdge,
  createRouterNode,
  normalizeRouterDefinition,
  publishedRoutersUsingModel,
} from "../../../frontend/src/components/model-router/router-definition.ts"

const singleBranchCondition = (label) => {
  const start = createRouterNode("start", { x: 0, y: 0 })
  const condition = createRouterNode("condition", { x: 240, y: 0 })
  const output = createRouterNode("output", { x: 480, y: 0 })
  return {
    schema_version: 2,
    presentation_version: 6,
    nodes: [start, condition, output],
    edges: [
      createRouterEdge(start.id, condition.id),
      createRouterEdge(condition.id, output.id, label),
    ],
  }
}

test("a drafted Condition with only its Match branch survives reload", () => {
  const normalized = normalizeRouterDefinition(singleBranchCondition("Match"))

  assert.deepEqual(normalized.nodes.map((node) => node.type), ["start", "condition", "output"])
  assert.deepEqual(normalized.edges.map((edge) => edge.label), ["", "Match"])
})

test("a legacy unlabeled pass-through Condition is still collapsed", () => {
  const normalized = normalizeRouterDefinition(singleBranchCondition(""))

  assert.deepEqual(normalized.nodes.map((node) => node.type), ["start", "output"])
  assert.equal(normalized.edges.length, 1)
})

test("a duplicate's prefilled name uses the reader's language", () => {
  // The name is an input value the page translator never rewrites.
  assert.equal(copyName("Unit Router", "en"), "Unit Router copy")
  assert.equal(copyName("Unit Router", "ja"), "Unit Router コピー")
  assert.equal(copyName("Unit Router", "ko"), "Unit Router 사본")
  assert.equal(copyName("Unit Router", "zh-CN"), "Unit Router 副本")
})

test("deleting a model names the published Routers that can still send to it", () => {
  const node = (fields) => ({ routing_model_id: null, candidates: [], model_configs: [], ...fields })
  const router = (name, status, fields) => ({ name, status, definition: { nodes: [node(fields)] } })
  const routers = [
    router("Candidate", "published", { candidates: [{ model_id: "m1" }] }),
    router("Strategy", "published", { model_configs: [{ model_id: "m1" }] }),
    router("Decision", "published", { routing_model_id: "m1" }),
    router("Draft", "draft", { candidates: [{ model_id: "m1" }] }),
    router("Other", "published", { candidates: [{ model_id: "m2" }] }),
  ]
  assert.deepEqual(publishedRoutersUsingModel(routers, "m1"), ["Candidate", "Strategy", "Decision"])
})
