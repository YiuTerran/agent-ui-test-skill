const phases = new Set(['test', 'cleanup', 'recovery'])
const safeKey = /^[A-Za-z][A-Za-z0-9_.:-]{0,79}$/

function limit(value, fallback, maximum, name) {
  const result = value ?? fallback
  if (!Number.isInteger(result) || result < 1 || result > maximum)
    throw new TypeError(`Invalid ui diagnostics ${name}`)
  return result
}

export function withUiDiagnostics(baseTest, options = {}) {
  const maxEvents = limit(options.maxEvents, 200, 200, 'maxEvents')
  const maxBytes = limit(options.maxBytes, 65536, 65536, 'maxBytes')
  if (maxBytes < 1024)
    throw new TypeError('Invalid ui diagnostics maxBytes')
  if (options.routeKey !== undefined && typeof options.routeKey !== 'function')
    throw new TypeError('Invalid ui diagnostics routeKey')

  const test = baseTest.extend({
    uiDiagnostics: [async ({ context }, use, testInfo) => {
      const started = performance.now()
      const events = []
      const active = new Map()
      const pages = new Map()
      const listeners = []
      let retainedBytes = 0
      let dropped = 0
      let diagnosticErrors = 0
      let serial = 0
      let closed = false

      function record(type, fields = {}) {
        if (closed) return
        const event = { type, elapsed_ms: Math.round(performance.now() - started), ...fields }
        if (active.size === 1) {
          const step = active.values().next().value
          event.active_step_id = step.id
          event.phase = step.phase
        } else if (active.size > 1) {
          event.attribution = 'concurrent_steps'
        }
        const bytes = Buffer.byteLength(JSON.stringify(event)) + 1
        if (fields.outcome === 'failed') {
          while (events.length && (events.length >= maxEvents || retainedBytes + bytes > maxBytes - 512)) {
            const index = events.findIndex(item => item.outcome !== 'failed')
            if (index < 0) break
            retainedBytes -= Buffer.byteLength(JSON.stringify(events[index])) + 1
            events.splice(index, 1)
            dropped++
          }
        }
        if (events.length >= maxEvents || retainedBytes + bytes > maxBytes - 512) {
          dropped++
          return
        }
        events.push(event)
        retainedBytes += bytes
      }

      function listen(emitter, name, operation) {
        const handler = value => {
          try { operation(value) } catch { diagnosticErrors++ }
        }
        emitter.on(name, handler)
        listeners.push(() => emitter.off(name, handler))
      }

      function observePage(page) {
        if (pages.has(page)) return
        const pageId = pages.size + 1
        pages.set(page, pageId)
        listen(page, 'pageerror', () => record('page_error', { page_id: pageId }))
      }

      function routeFields(url) {
        if (!options.routeKey) return {}
        try {
          const key = options.routeKey(url)
          if (typeof key === 'string' && safeKey.test(key)) return { route_key: key }
          if (key !== undefined && key !== null) {
            diagnosticErrors++
            if (typeof key.then === 'function') Promise.resolve(key).catch(() => {})
          }
        } catch {
          diagnosticErrors++
        }
        return {}
      }

      try {
        for (const page of context.pages()) observePage(page)
        listen(context, 'page', observePage)
        listen(context, 'requestfailed', request =>
          record('request_failed', routeFields(request.url())))
        listen(context, 'response', response => {
          const status = response.status()
          if (Number.isInteger(status) && status >= 400 && status <= 599)
            record('http_error', { status, ...routeFields(response.url()) })
        })
      } catch {
        diagnosticErrors++
      }

      const diagnostics = {
        async step(id, operation, { phase = 'test' } = {}) {
          if (closed) throw new Error('UI diagnostics fixture is closed')
          if (typeof id !== 'string' || !safeKey.test(id) || !phases.has(phase)
              || typeof operation !== 'function')
            throw new TypeError('Use a static UI step ID, operation and supported phase')
          const token = ++serial
          active.set(token, { id, phase })
          record('step_start', { step_id: id, phase })
          try {
            const result = await test.step(id, operation)
            record('step_end', { step_id: id, phase, outcome: 'passed' })
            return result
          } catch (error) {
            record('step_end', { step_id: id, phase, outcome: 'failed' })
            throw error
          } finally {
            active.delete(token)
          }
        }
      }

      let fixtureError = false
      try {
        await use(diagnostics)
      } catch (error) {
        fixtureError = true
        throw error
      } finally {
        for (const remove of listeners) {
          try { remove() } catch { diagnosticErrors++ }
        }
        closed = true
        try {
          const status = testInfo.status
          const errors = Array.isArray(testInfo.errors) ? testInfo.errors.length : 0
          if (fixtureError || errors > 0 || ['failed', 'timedOut', 'interrupted'].includes(status)) {
            const report = {
              schema_version: 1,
              capture: 'bounded_metadata',
              status: ['passed', 'failed', 'timedOut', 'interrupted', 'skipped'].includes(status)
                ? status : 'unknown',
              retry: Number.isInteger(testInfo.retry) ? testInfo.retry : 0,
              repeat_each_index: Number.isInteger(testInfo.repeatEachIndex) ? testInfo.repeatEachIndex : 0,
              native_error_count: errors,
              diagnostic_error_count: diagnosticErrors,
              dropped_events: dropped,
              limits: { events: maxEvents, bytes: maxBytes },
              completeness: 'best_effort_until_fixture_teardown',
              events
            }
            let body = Buffer.from(JSON.stringify(report))
            while (body.length > maxBytes && events.length) {
              events.pop()
              report.dropped_events++
              body = Buffer.from(JSON.stringify(report))
            }
            await testInfo.attach('ui-diagnostics', { body, contentType: 'application/json' })
          }
        } catch {
        }
      }
    }, { auto: true }]
  })
  return test
}
