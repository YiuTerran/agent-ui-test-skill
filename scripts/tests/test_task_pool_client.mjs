import assert from 'node:assert/strict'
import childProcess, { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { syncBuiltinESMExports } from 'node:module'
import { dirname, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { budgetApi, holdTask, settleAll } from '../../assets/task-pool-client.mjs'

const scripts = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const python = process.env.PYTHON || 'python3'
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

async function fixture(t) {
  const project = await mkdtemp(resolve(tmpdir(), 'task-pool-client-'))
  const directory = resolve(project, 'pool')
  const script = [
    'import sys',
    'sys.path.insert(0, sys.argv[1])',
    'from task_pool import Monitor, configuration',
    "config = configuration(sys.argv[2], 'run', 'cli', 2, 2)",
    "with Monitor(sys.argv[3], config, sampler=lambda _: dict(cpu_percent=10, memory_percent=10)):",
    " print('READY', flush=True)",
    ' sys.stdin.read()',
  ].join('\n')
  const monitor = spawn(python, ['-c', script, scripts, project, directory],
    { stdio: ['pipe', 'pipe', 'inherit'] })
  const closed = once(monitor, 'close')
  const lines = createInterface({ input: monitor.stdout })
  await Promise.race([
    once(lines, 'line').then(([line]) => assert.equal(line, 'READY')),
    closed.then(() => { throw new Error('offline_monitor_failed') }),
  ])
  const clients = []
  const helpers = []
  t.after(async () => {
    await Promise.allSettled(clients.map(client => client.release()))
    monitor.stdin.end()
    await closed
    lines.close()
    await rm(project, { recursive: true, force: true })
  })
  return {
    directory, monitor, closed,
    async client() {
      const originalSpawn = childProcess.spawn
      childProcess.spawn = (...args) => {
        const child = originalSpawn(...args)
        if (args[1].includes('hold')) helpers.push(child)
        return child
      }
      syncBuiltinESMExports()
      let client
      try {
        client = await holdTask({
          python, poolScript: resolve(scripts, 'task_pool.py'), project,
          directory, run: 'run', stage: 'cli', waitSeconds: 3,
        })
      }
      finally {
        childProcess.spawn = originalSpawn
        syncBuiltinESMExports()
      }
      clients.push(client)
      return client
    },
    helpers,
  }
}

test('API proxies in different helpers share one ordinary leaf budget', { timeout: 10000 }, async t => {
  const setup = await fixture(t)
  const first = await setup.client()
  const second = await setup.client()
  let active = 0
  let peak = 0
  const raw = {
    name: 'bound-target',
    async post(value) {
      assert.equal(this.name, 'bound-target')
      active++
      peak = Math.max(peak, active)
      await delay(15)
      active--
      return value
    },
    storageState() { return this.name },
  }
  const apis = [budgetApi(raw, first), budgetApi(raw, second)]
  assert.equal(apis[0].storageState(), 'bound-target')
  const results = await settleAll(Array.from({ length: 8 }, (_, index) => apis[index % 2].post(index)))
  assert.deepEqual(results, [0, 1, 2, 3, 4, 5, 6, 7])
  assert.equal(peak, 1)
})

test('a device reply progresses while a synchronous command holds ordinary IO', { timeout: 10000 }, async t => {
  const setup = await fixture(t)
  const client = await setup.client()
  const commandEntered = deferred()
  const reply = deferred()
  const command = client.runIO(async () => {
    commandEntered.resolve()
    return reply.promise
  })
  await commandEntered.promise
  await client.runIO(async () => reply.resolve('acknowledged'), true)
  assert.equal(await command, 'acknowledged')
})

test('release waits for all running branches and prevents new operations', { timeout: 10000 }, async t => {
  const setup = await fixture(t)
  const client = await setup.client()
  const entered = deferred()
  const finish = deferred()
  const operation = client.runIO(async () => {
    entered.resolve()
    await finish.promise
  })
  await entered.promise
  let released = false
  const release = client.release().then(() => { released = true })
  await delay(40)
  assert.equal(released, false)
  assert.throws(() => client.runIO(async () => {}), /not_active/)
  finish.resolve()
  await operation
  await release
  assert.equal(released, true)
})

test('settleAll preserves failures and waits for a slower writing branch', async () => {
  let finished = false
  await assert.rejects(settleAll([
    Promise.reject(new Error('first_failure')),
    delay(30).then(() => { finished = true; throw new Error('second_failure') }),
  ]), error => {
    assert.equal(finished, true)
    assert.deepEqual(error.errors.map(failure => failure.message), ['first_failure', 'second_failure'])
    return true
  })
})

test('a failed leaf releases its slot for subsequent work and cleanup', { timeout: 10000 }, async t => {
  const setup = await fixture(t)
  const client = await setup.client()
  await assert.rejects(client.runIO(async () => { throw new Error('synthetic_request_failure') }),
    /synthetic_request_failure/)
  assert.equal(await client.runIO(async () => 'cleanup_completed'), 'cleanup_completed')
})

test('stopped monitor rejects pending IO and blocks new business in its own stage', { timeout: 10000 }, async t => {
  const setup = await fixture(t)
  const client = await setup.client()
  setup.monitor.stdin.end()
  await setup.closed
  await assert.rejects(client.runIO(async () => assert.fail('must not dispatch')), /helper_lost/)
  await assert.rejects(client.release(), /helper_lost/)
  const marker = JSON.parse(await readFile(resolve(setup.directory, 'dispatch-blocked.json'), 'utf8'))
  assert.equal(marker.reason, 'task_pool_helper_lost')
})

test('missing helper executable rejects without waiting for admission', { timeout: 3000 }, async () => {
  await assert.rejects(holdTask({
    python: resolve(tmpdir(), 'missing-task-pool-python'),
    poolScript: resolve(scripts, 'task_pool.py'), directory: tmpdir(), project: tmpdir(),
    run: 'run', stage: 'cli', waitSeconds: 1,
  }), /ENOENT/)
})

test('helper loss quarantines ordinary IO while explicit cleanup and existing replies finish',
  { timeout: 10000 }, async t => {
    const setup = await fixture(t)
    const first = await setup.client()
    const second = await setup.client()
    const entered = deferred()
    const finish = deferred()
    const replyEntered = deferred()
    const finishReply = deferred()
    let active = 0
    let peak = 0
    const businessError = new Error('specific_business_failure')
    const operation = first.runIO(async () => {
      active++
      peak = Math.max(peak, active)
      entered.resolve()
      await finish.promise
      active--
      throw businessError
    })
    const reply = second.runIO(async () => {
      replyEntered.resolve()
      await finishReply.promise
      return 'existing_reply_finished'
    }, true)
    try {
      await Promise.all([entered.promise, replyEntered.promise])
      const helperClosed = once(setup.helpers[0], 'close')
      setup.helpers[0].kill()
      await helperClosed
      await assert.rejects(second.runIO(async () => {
        active++
        peak = Math.max(peak, active)
        assert.fail('ordinary_business_must_not_dispatch')
      }), /pool_dispatch_blocked/)
      assert.equal(peak, 1)
      assert.equal(active, 1)
      finishReply.resolve()
      assert.equal(await reply, 'existing_reply_finished')
      assert.equal(await second.runCleanupIO(async () => 'owned_cleanup_finished'), 'owned_cleanup_finished')
      finish.resolve()
      await assert.rejects(operation, error => {
        assert.ok(error instanceof AggregateError)
        assert.equal(error.errors[0], businessError)
        assert.match(error.errors[1].message, /helper_lost/)
        return true
      })
      await second.release()
      await assert.rejects(first.release(), /helper_lost/)
    }
    finally {
      finish.resolve()
      finishReply.resolve()
      await Promise.allSettled([operation, reply])
    }
  })

test('successful operation still reports a lost release acknowledgement', { timeout: 10000 }, async t => {
  const setup = await fixture(t)
  const client = await setup.client()
  await assert.rejects(client.runIO(async () => {
    const closed = once(setup.helpers[0], 'close')
    setup.helpers[0].kill()
    await closed
    return 'operation_finished'
  }), /helper_lost/)
})
