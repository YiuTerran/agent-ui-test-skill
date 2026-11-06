import { spawn } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createInterface } from 'node:readline'

export async function settleAll(tasks) {
  const results = await Promise.allSettled(tasks)
  const failures = results.filter(result => result.status === 'rejected')
  if (failures.length) throw new AggregateError(failures.map(result => result.reason), 'parallel_operations_failed')
  return results.map(result => result.value)
}

export function budgetApi(api, admission) {
  const methods = new Set(['fetch', 'get', 'post', 'put', 'patch', 'delete', 'head'])
  return new Proxy(api, {
    get(target, key) {
      const value = Reflect.get(target, key)
      if (typeof value !== 'function') return value
      if (methods.has(key)) return (...args) => admission.runIO(() => value.apply(target, args))
      return value.bind(target)
    },
  })
}

export async function holdTask({
  python = 'python3', poolScript, directory, project, run, stage,
  cleanup = false, waitSeconds = 900,
}) {
  if (![poolScript, directory, project, run, stage].every(value => typeof value === 'string' && value))
    throw new Error('task_pool_context_required')
  if (!Number.isFinite(waitSeconds) || waitSeconds <= 0) throw new Error('task_pool_invalid_timeout')
  const child = spawn(python, [
    poolScript, 'hold', '--directory', directory, '--project', project, '--run', run,
    '--stage', stage, '--wait-seconds', String(waitSeconds), ...(cleanup ? ['--cleanup'] : []),
  ], { stdio: ['pipe', 'pipe', 'pipe'] })
  let failure
  let admitted = false
  let closing = false
  let sequence = 0
  let releasePromise
  let resolveReady
  let rejectReady
  const pending = new Map()
  const active = new Set()
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject })
  const lost = error => {
    failure ||= error || new Error('task_pool_helper_lost')
    rejectReady(failure)
    for (const waiter of pending.values()) waiter.reject(failure)
    pending.clear()
    if (admitted) {
      try {
        writeFileSync(resolve(directory, 'dispatch-blocked.json'),
          JSON.stringify({ reason: 'task_pool_helper_lost' }), { flag: 'wx', mode: 0o600 })
      }
      catch (error) {
        if (error.code !== 'EEXIST') failure = new Error('task_pool_block_marker_failed', { cause: error })
      }
    }
  }
  const closed = new Promise(resolve => {
    child.once('error', error => { lost(error); resolve() })
    child.once('close', code => {
      if (!closing || code !== 0) lost(new Error('task_pool_helper_lost'))
      resolve()
    })
  })
  child.stdin.on('error', lost)
  child.stderr.resume()
  const lines = createInterface({ input: child.stdout })
  lines.on('line', line => {
    try {
      const message = JSON.parse(line)
      if (!admitted && message.event === 'admitted') {
        admitted = true
        resolveReady()
        return
      }
      const rejected = message.event === 'rejected' && message.error === 'pool_dispatch_blocked'
      const key = `${message.id}:${rejected ? 'acquired' : message.event}`
      const waiter = pending.get(key)
      if (!waiter) throw new Error('task_pool_unexpected_reply')
      pending.delete(key)
      if (rejected) waiter.reject(new Error(message.error))
      else waiter.resolve()
    }
    catch (error) { lost(error) }
  })
  const exchange = (id, action, event, progress = false) => new Promise((resolve, reject) => {
    if (failure) { reject(failure); return }
    pending.set(`${id}:${event}`, { resolve, reject })
    child.stdin.write(JSON.stringify({ id, action, progress }) + '\n')
  })
  const release = () => {
    releasePromise ||= (async () => {
      closing = true
      await Promise.allSettled([...active])
      child.stdin.end()
      const timer = setTimeout(() => child.kill(), 2000)
      try { await closed }
      finally { clearTimeout(timer); lines.close() }
      if (failure) throw failure
    })()
    return releasePromise
  }
  const timer = setTimeout(() => lost(new Error('task_pool_wait_timeout')), (waitSeconds + 5) * 1000)
  try { await ready }
  catch (error) {
    await release().catch(() => {})
    throw error
  }
  finally { clearTimeout(timer) }
  const assertActive = () => {
    if (!admitted || closing || failure) throw failure || new Error('task_pool_not_active')
  }
  const runIO = (operation, progress = false) => {
    assertActive()
    const id = String(++sequence)
    const operationPromise = (async () => {
      await exchange(id, 'acquire', 'acquired', progress)
      let result
      let operationError
      let failed = false
      try { result = await operation() }
      catch (error) { operationError = error; failed = true }
      try { await exchange(id, 'release', 'released') }
      catch (error) {
        if (failed) throw new AggregateError([operationError, error], 'task_pool_operation_and_release_failed')
        throw error
      }
      if (failed) throw operationError
      return result
    })()
    active.add(operationPromise)
    void operationPromise.finally(() => active.delete(operationPromise)).catch(() => {})
    return operationPromise
  }
  return { runIO, runCleanupIO: operation => runIO(operation, true), release, assertActive }
}
