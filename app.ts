import path from 'node:path'
import Fastify from 'fastify'
import cors from '@fastify/cors'
import compress from '@fastify/compress'
import staticFiles from '@fastify/static'

const app = Fastify()
const port = Number(process.env.PORT || 12315)

if (process.argv.length > 2) {
    throw new Error('The static server takes no command-line options; use PORT to set the port')
}

if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error('PORT must be an integer between 0 and 65535')
}

await app.register(cors, {
    methods: ['GET', 'HEAD'],
    strictPreflight: false,
})
await app.register(compress, {
    globalDecompression: false,
    encodings: ['br', 'gzip', 'deflate'],
})

app.get('/', (_request, reply) => {
    return reply.redirect('https://github.com/anosu/girlscreaionr-translation')
})

app.setErrorHandler((error, _request, reply) => {
    // Keep inaccessible static paths consistent with missing files.
    if (error instanceof Error && 'statusCode' in error && error.statusCode === 403) {
        return reply.callNotFound()
    }
    return reply.send(error)
})

await app.register(staticFiles, {
    root: path.join(import.meta.dirname, 'translations'),
    prefix: '/translations/',
    index: false,
    redirect: false,
    dotfiles: 'ignore',
    maxAge: 0,
})

await app.listen({ port, host: '::' })
const address = app.server.address()
if (address && typeof address !== 'string') {
    console.log(`Server is running on http://localhost:${address.port}`)
}
