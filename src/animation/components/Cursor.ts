import * as THREE from "three";
import {BufferGeometry, Color} from "three";
import {MeshLine, MeshLineMaterial} from 'meshline';

export default class Cursor extends THREE.Mesh
{
    constructor(conf: {radius: number, color: Color, width: number})
    {
        // https://discourse.threejs.org/t/shift-vertices-of-circle-geometry-not-working/26664
        const pts = new THREE.Path().absarc(0, 0, conf.radius, 0, Math.PI * 2, true).getPoints(90)
        const geometry = new MeshLine()
        geometry.setGeometry(new BufferGeometry().setFromPoints(pts), () => 1)

        // MeshLine: https://stackoverflow.com/a/25759280/7346633
        const material = new MeshLineMaterial({resolution: new THREE.Vector2(window.innerWidth, window.innerHeight),
            color: conf.color, lineWidth: 1})
        super(geometry, material)
    }
}
