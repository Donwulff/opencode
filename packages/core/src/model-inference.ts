export * as ModelInference from "./model-inference.js"

import type { LLMRequest } from "@opencode/ai/effect"
import type { Effect } from "effect"
import { KeyedMutex } from "./effect/keyed-mutex.js"
import type { ModelResolver } from "./model-resolver.js"

const requests = KeyedMutex.makeUnsafe<string>()

/** Hold the permit through response consumption for requests sharing a backend and cache affinity. */
export const serialize =
  (model: ModelResolver.Resolved, request: LLMRequest) =>
  <A, E, R>(effect: Effect.Effect<A, E, R>): Effect.Effect<A, E, R> => {
    if (!model.foregroundOnly || !request.promptCacheKey) return effect
    const endpoint =
      request.model.route.protocol === "ai-sdk" ? model.inferenceBaseURL : request.model.route.endpoint.baseURL
    const backend = endpoint ? `endpoint:${endpoint.replace(/\/+$/, "")}` : `provider:${model.ref.providerID}`
    return requests.withLock(JSON.stringify([backend, request.promptCacheKey]))(effect)
  }
