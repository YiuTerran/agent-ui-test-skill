import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import test from 'node:test'
import { withUiDiagnostics } from '../../assets/ui-diagnostics.mjs'

function setup(options = {}) {
  const page = new EventEmitter()
  const context = new EventEmitter()
  context.pages = () => [page]
  const attachments = []
  const nativeSteps = []
  let fixture
  const baseTest = {
    projectFixture: 'preserved',
    extend(definitions) {
      fixture = definitions.uiDiagnostics[0]
      assert.equal(definitions.uiDiagnostics[1].auto, true)
      return Object.assign(Object.create(this), {
        step: async (id, operation) => {
          nativeSteps.push(id)
          return operation()
        }
      })
    }
  }
  const extended = withUiDiagnostics(baseTest, options)
  const info = {
    status: 'passed', errors: [], retry: 0, repeatEachIndex: 0,
    attach: async (name, attachment) => attachments.push({ name, ...attachment })
  }
  return { context, page, attachments, info, extended, nativeSteps,
    run: operation => fixture({ context }, operation, info),
    report: () => JSON.parse(attachments[0].body.toString()) }
}

test('success preserves fixtures and native steps without attaching errors', async () => {
  const harness = setup()
  assert.equal(harness.extended.projectFixture, 'preserved')
  await harness.run(async diagnostics => {
    assert.equal(await diagnostics.step('orders.create', async () => 42), 42)
    harness.context.emit('response', { status: () => 400, url: () => 'sensitive-url' })
    harness.page.emit('pageerror', new Error('expected application error'))
  })
  assert.equal(harness.attachments.length, 0)
  assert.deepEqual(harness.nativeSteps, ['orders.create'])
  assert.equal(harness.page.listenerCount('pageerror'), 0)
  assert.equal(harness.context.eventNames().length, 0)
})

test('native failure and cleanup failure retain distinct phases and error identity', async () => {
  const harness = setup()
  const business = new Error('original business failure')
  const cleanup = new Error('original cleanup failure')
  await harness.run(async diagnostics => {
    await assert.rejects(diagnostics.step('orders.create', async () => { throw business }),
      error => error === business)
    await assert.rejects(diagnostics.step('orders.remove', async () => { throw cleanup },
      { phase: 'cleanup' }), error => error === cleanup)
    harness.info.status = 'failed'
    harness.info.errors = [business, cleanup]
  })
  assert.equal(harness.report().native_error_count, 2)
  assert.deepEqual(harness.report().events.filter(event => event.outcome === 'failed')
    .map(event => [event.step_id, event.phase]), [['orders.create', 'test'], ['orders.remove', 'cleanup']])
})

test('default evidence excludes sensitive path query messages headers body and console', async () => {
  const canary = 'SECRET_CANARY_PRIVATE'
  const harness = setup()
  await harness.run(async diagnostics => {
    harness.context.emit('requestfailed', {
      url: () => `https://host/${canary}?token=${canary}#${canary}`,
      failure: () => ({ errorText: canary }), headers: () => ({ Authorization: canary }),
      postData: () => canary
    })
    harness.context.emit('response', { status: () => 503,
      url: () => `https://${canary}`, text: () => canary })
    harness.page.emit('pageerror', new Error(canary))
    harness.page.emit('console', { text: () => canary })
    try {
      await diagnostics.step('orders.read', async () => { throw new Error(canary) })
    } catch (error) {
      harness.info.status = 'failed'
      harness.info.errors = [error]
    }
  })
  assert.ok(!harness.attachments[0].body.toString().includes(canary))
  assert.deepEqual(harness.report().events.slice(0, 3).map(event => event.type),
    ['request_failed', 'http_error', 'page_error'])
  assert.ok(harness.report().events.every(event => !('url' in event)))
})

test('safe project route keys are optional and callback errors are isolated', async () => {
  const harness = setup({ routeKey: url => {
    if (url === 'broken') throw new Error('secret')
    return url === 'safe' ? 'orders.list' : '/private?secret'
  } })
  await harness.run(async () => {
    for (const url of ['safe', 'unsafe', 'broken'])
      harness.context.emit('requestfailed', { url: () => url })
    harness.info.status = 'failed'
  })
  assert.equal(harness.report().events[0].route_key, 'orders.list')
  assert.equal(harness.report().diagnostic_error_count, 2)
  assert.ok(!harness.attachments[0].body.toString().includes('private'))
})

test('popups are observed once and all listeners are removed on closed pages', async () => {
  const harness = setup()
  const popup = new EventEmitter()
  await harness.run(async () => {
    harness.context.emit('page', popup)
    harness.context.emit('page', popup)
    popup.emit('pageerror', new Error('not retained'))
    harness.info.status = 'timedOut'
  })
  assert.equal(harness.report().events[0].page_id, 2)
  assert.equal(harness.report().events.length, 1)
  assert.equal(popup.listenerCount('pageerror'), 0)
  popup.emit('pageerror', new Error('late'))
  assert.equal(harness.report().events.length, 1)
})

test('an accidentally async route mapper cannot produce an unhandled rejection', async () => {
  const harness = setup({ routeKey: async () => { throw new Error('private mapper failure') } })
  await harness.run(async () => {
    harness.context.emit('requestfailed', { url: () => 'private' })
    await new Promise(resolve => setImmediate(resolve))
    harness.info.status = 'failed'
  })
  assert.equal(harness.report().diagnostic_error_count, 1)
  assert.equal(harness.report().events[0].type, 'request_failed')
  assert.ok(!harness.attachments[0].body.toString().includes('private'))
})

test('event count and serialized bytes are bounded with explicit truncation', async () => {
  for (const options of [{ maxEvents: 3 }, { maxBytes: 1024 }]) {
    const harness = setup(options)
    await harness.run(async diagnostics => {
      for (let index = 0; index < 250; index++)
        await diagnostics.step('orders.read', async () => {})
      harness.info.status = 'failed'
    })
    assert.ok(harness.report().dropped_events > 0)
    assert.ok(harness.report().events.length <= (options.maxEvents || 200))
    assert.ok(harness.attachments[0].body.length <= (options.maxBytes || 65536))
  }
})

test('attachment failure never masks original operation or fixture errors', async () => {
  const harness = setup()
  const original = new Error('business')
  harness.info.attach = async () => { throw new Error('attachment write failed') }
  await assert.rejects(harness.run(async diagnostics =>
    diagnostics.step('orders.create', async () => { throw original })), error => error === original)
  assert.equal(harness.context.eventNames().length, 0)
})

test('late failure is retained after ordinary metadata reaches the limit', async () => {
  const harness = setup({ maxEvents: 3 })
  await harness.run(async diagnostics => {
    for (let index = 0; index < 10; index++)
      await diagnostics.step('orders.read', async () => {})
    try {
      await diagnostics.step('orders.submit', async () => { throw new Error('business') })
    } catch (error) {
      harness.info.errors = [error]
      harness.info.status = 'failed'
    }
  })
  assert.ok(harness.report().events.some(event =>
    event.step_id === 'orders.submit' && event.outcome === 'failed'))
  assert.ok(harness.report().dropped_events > 0)
})

test('teardown errors attach without requiring a failed wrapper step', async () => {
  const harness = setup()
  await harness.run(async () => {
    harness.info.errors = [new Error('late cleanup')]
    harness.info.retry = 2
    harness.info.repeatEachIndex = 1
  })
  assert.equal(harness.report().native_error_count, 1)
  assert.equal(harness.report().retry, 2)
  assert.equal(harness.report().repeat_each_index, 1)
  assert.equal(harness.report().completeness, 'best_effort_until_fixture_teardown')
})

test('concurrent steps do not claim unique event causality', async () => {
  const harness = setup()
  await harness.run(async diagnostics => {
    await Promise.all([
      diagnostics.step('orders.one', async () => {
        await new Promise(resolve => setImmediate(resolve))
        harness.page.emit('pageerror', new Error('concurrent'))
      }),
      diagnostics.step('orders.two', async () => new Promise(resolve => setImmediate(resolve)))
    ])
    harness.info.status = 'failed'
  })
  const event = harness.report().events.find(event => event.type === 'page_error')
  assert.equal(event.attribution, 'concurrent_steps')
  assert.ok(!('active_step_id' in event))
})

test('invalid options and unsafe dynamic step labels fail explicitly', async () => {
  for (const options of [{ maxEvents: 201 }, { maxBytes: 100 }, { routeKey: 'invalid' }])
    assert.throws(() => setup(options), TypeError)
  const harness = setup()
  await harness.run(async diagnostics => {
    await assert.rejects(diagnostics.step('https://secret?token=value', async () => {}), TypeError)
    await assert.rejects(diagnostics.step('safe', async () => {}, { phase: 'invalid' }), TypeError)
  })
})
