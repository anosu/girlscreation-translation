import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { Server } from 'node:http'
import test from 'node:test'

test('Vercel can load the entrypoint before starting its captured server', { timeout: 10000 }, async (t) => {
    const originalListen = Server.prototype.listen
    const captured = Promise.withResolvers()
    let server
    let timer
    t.after(() => {
        clearTimeout(timer)
        Server.prototype.listen = originalListen
        server?.closeAllConnections()
        server?.close()
    })

    // Vercel captures listen() during import and starts the server afterwards.
    Server.prototype.listen = function () {
        server = this
        Server.prototype.listen = originalListen
        captured.resolve(this)
        return this
    }
    await Promise.race([
        import('../app.ts'),
        new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error('Entrypoint waited for listen() during import')), 5000)
        }),
    ])
    clearTimeout(timer)
    await captured.promise
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
    const base = `http://127.0.0.1:${server.address().port}`

    const response = await fetch(`${base}/translations/zh-Hans/names.json`)
    assert.equal(response.status, 200)
    assert.equal(await response.text(), await readFile(new URL('../translations/zh-Hans/names.json', import.meta.url), 'utf8'))
    const home = await fetch(base, { redirect: 'manual' })
    assert.equal(home.status, 302)
    assert.equal(home.headers.get('location'), 'https://github.com/anosu/girlscreation-translation')
    assert.equal((await fetch(`${base}/translations/nonexistent.json`)).status, 404)
})
