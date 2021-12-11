import * as THREE from 'three'

/**
 * Create a 3D box geometry made out of dashed lines
 * @param width
 * @param height
 * @param depth
 */
export function box(width: number, height: number, depth: number): THREE.BufferGeometry
{
    width = width * 0.5
    height = height * 0.5
    depth = depth * 0.5

    const geometry = new THREE.BufferGeometry();
    const position = [];

    for (const x of [-1, 1])
        for (const y of [-1, 1])
            for (const z of [-1, 1])
            {
                const rx = x * width, ry = y * height, rz = z * depth
                position.push(rx, ry, rz)
                position.push(rx * -x, ry, rz)
                position.push(rx, ry, rz)
                position.push(rx, ry * -y, rz)
                position.push(rx, ry, rz)
                position.push(rx, ry, rz * -z)
            }

    geometry.setAttribute('position', new THREE.Float32BufferAttribute(position, 3))
    return geometry
}

/**
 * Create a 2D circle
 * @param color
 * @param z
 * @param r
 * @param hollow
 */
export function circle(color: THREE.Color | number | string, z: number, r: number): THREE.Mesh
{
    const geometry = new THREE.CircleGeometry(r, 64)
    const material = new THREE.MeshBasicMaterial({color})
    const circle = new THREE.Mesh(geometry, material)
    circle.position.z = z
    return circle
}

/**
 * Ignore depth https://stackoverflow.com/a/62818553/7346633
 * @param obj
 * @param material
 */
export function alwaysOnTop(obj: THREE.Object3D, material: THREE.Material): void
{
    obj.renderOrder = 999
    material.depthTest = false
    material.depthWrite = false
    obj.onBeforeRender = (r) => r.clearDepth()
}
