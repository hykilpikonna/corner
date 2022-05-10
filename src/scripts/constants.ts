const production = true

export const hosts = {
    api: 'https://profile-api.hydev.org',
    content: production ? 'https://profile-content.hydev.org' : 'http://localhost:8082'
}

export const $ = (window as any).jQuery
