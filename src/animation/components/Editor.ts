import {Mesh, Object3D} from "three";
import IUpdatable from "@/animation/components/IUpdatable";
import {circle} from "@/animation/Helpers";
import {camera, scene} from "@/animation/Home";
import {moused, projectTo3D} from "@/animation/Trackers";
import {config} from "@/animation/Config";
import {minMax} from "@/utils";

export default class Editor implements IUpdatable
{
    hand: Mesh
    color = '#664400'
    radius = 3
    scale = 1
    z = config.editor.zMax

    constructor()
    {
        this.hand = circle(this.color, 0, this.radius)
        scene.add(this.hand)

        window.addEventListener('wheel', (e) =>
        {
            let direction = (e.detail < 0 || e.deltaY > 0) ? 1 : -1;

            // Shift to micro-adjust
            if (e.shiftKey) direction /= 10

            // Ctrl + Alt to shift the entire plane
            if (e.altKey && e.ctrlKey)
            {
                // TODO
                return
            }

            // Scroll to adjust z, alt + scroll to adjust radius
            if (e.altKey)
            {
                this.scale -= direction / 10
                this.hand.scale.set(this.scale, this.scale, this.scale)
            }
            else this.z = minMax(this.z - direction, config.editor.zMin, config.editor.zMax)

        }, false)
    }

    update(dt: number): void
    {
        const pos = projectTo3D(moused.x, moused.y, this.z)
        this.hand.position.copy(pos)
        return
    }
}
