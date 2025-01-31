import numpy as np
import tensorflow as tf
import tensorflow_addons as tfa
import cv2
import argparse
from tqdm import tqdm
import os
import time
import random
import pathlib

TRAIN_EPOCHS = 1000
im_sz = 1024
mp_sz = 96

warp_scale = 0.05
mult_scale = 0.4
add_scale = 0.4
add_first = False


@tf.function
def warp(origins, targets, preds_org, preds_trg):
    if add_first:
        res_targets = tfa.image.dense_image_warp((origins + preds_org[:, :, :, 3:6] * 2 * add_scale) * tf.maximum(0.1,
                                                                                                                  1 + preds_org[
                                                                                                                      :,
                                                                                                                      :,
                                                                                                                      :,
                                                                                                                      0:3] * mult_scale),
                                                 preds_org[:, :, :, 6:8] * im_sz * warp_scale)
        res_origins = tfa.image.dense_image_warp((targets + preds_trg[:, :, :, 3:6] * 2 * add_scale) * tf.maximum(0.1,
                                                                                                                  1 + preds_trg[
                                                                                                                      :,
                                                                                                                      :,
                                                                                                                      :,
                                                                                                                      0:3] * mult_scale),
                                                 preds_trg[:, :, :, 6:8] * im_sz * warp_scale)
    else:
        res_targets = tfa.image.dense_image_warp(
            origins * tf.maximum(0.1, 1 + preds_org[:, :, :, 0:3] * mult_scale) + preds_org[:, :, :,
                                                                                  3:6] * 2 * add_scale,
            preds_org[:, :, :, 6:8] * im_sz * warp_scale)
        res_origins = tfa.image.dense_image_warp(
            targets * tf.maximum(0.1, 1 + preds_trg[:, :, :, 0:3] * mult_scale) + preds_trg[:, :, :,
                                                                                  3:6] * 2 * add_scale,
            preds_trg[:, :, :, 6:8] * im_sz * warp_scale)

    return res_targets, res_origins


def create_grid(scale):
    grid = np.mgrid[0:scale, 0:scale] / (scale - 1) * 2 - 1
    grid = np.swapaxes(grid, 0, 2)
    grid = np.expand_dims(grid, axis=0)
    return grid


def produce_warp_maps(origins, targets, output_folder="morph"):
    class MyModel(tf.keras.Model):
        def __init__(self):
            super(MyModel, self).__init__()
            self.conv1 = tf.keras.layers.Conv2D(64, (5, 5))
            self.act1 = tf.keras.layers.LeakyReLU(alpha=0.2)
            self.conv2 = tf.keras.layers.Conv2D(64, (5, 5))
            self.act2 = tf.keras.layers.LeakyReLU(alpha=0.2)
            self.convo = tf.keras.layers.Conv2D((3 + 3 + 2) * 2, (5, 5))

        def call(self, maps):
            x = tf.image.resize(maps, [mp_sz, mp_sz])
            x = self.conv1(x)
            x = self.act1(x)
            x = self.conv2(x)
            x = self.act2(x)
            x = self.convo(x)
            return x

    model = MyModel()

    loss_object = tf.keras.losses.MeanSquaredError()
    optimizer = tf.keras.optimizers.Adam(learning_rate=0.0002)

    train_loss = tf.keras.metrics.Mean(name='train_loss')

    @tf.function
    def train_step(maps, origins, targets):
        with tf.GradientTape() as tape:
            preds = model(maps)
            preds = tf.image.resize(preds, [im_sz, im_sz])

            # a = tf.random.uniform([maps.shape[0]])
            # res_targets, res_origins = warp(origins, targets, preds[...,:8] * a, preds[...,8:] * (1 - a))
            res_targets_, res_origins_ = warp(origins, targets, preds[..., :8], preds[..., 8:])

            res_map = tfa.image.dense_image_warp(maps, preds[:, :, :,
                                                       6:8] * im_sz * warp_scale)  # warp maps consistency checker
            res_map = tfa.image.dense_image_warp(res_map, preds[:, :, :, 14:16] * im_sz * warp_scale)

            loss = loss_object(maps, res_map) * 1 + loss_object(res_targets_, targets) * 0.3 + loss_object(res_origins_,
                                                                                                           origins) * 0.3

        gradients = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(gradients, model.trainable_variables))

        train_loss(loss)

    maps = create_grid(im_sz)
    maps = np.concatenate((maps, origins * 0.1, targets * 0.1), axis=-1).astype(np.float32)

    epoch = 0
    template = 'Epoch {}, Loss: {}'

    t = tqdm(range(TRAIN_EPOCHS), desc=template.format(epoch, train_loss.result()))

    for i in t:
        epoch = i + 1

        t.set_description(template.format(epoch, train_loss.result()))
        t.refresh()

        train_step(maps, origins, targets)

        if (epoch < 100 and epoch % 10 == 0) or \
                (epoch < 1000 and epoch % 100 == 0) or \
                (epoch % 1000 == 0):
            preds = model(maps, training=False)[:1]
            preds = tf.image.resize(preds, [im_sz, im_sz])

            res_targets, res_origins = warp(origins, targets, preds[..., :8], preds[..., 8:])

            res_targets = tf.clip_by_value(res_targets, -1, 1)[0]
            res_img = ((res_targets.numpy() + 1) * 127.5).astype(np.uint8)
            cv2.imwrite("train/a_to_b_%d.jpg" % epoch, cv2.cvtColor(res_img, cv2.COLOR_RGB2BGR))

            res_origins = tf.clip_by_value(res_origins, -1, 1)[0]
            res_img = ((res_origins.numpy() + 1) * 127.5).astype(np.uint8)
            cv2.imwrite("train/b_to_a_%d.jpg" % epoch, cv2.cvtColor(res_img, cv2.COLOR_RGB2BGR))
            if output_folder:
                np.save(os.path.join(output_folder, 'preds.npy'), preds.numpy())
            else:
                np.save('preds.npy', preds.numpy())


def use_warp_maps(origins, targets, fps, steps, output_folder="morph"):
    STEPS = steps

    if output_folder:
        preds = np.load(os.path.join(output_folder, 'preds.npy'))
    else:
        preds = np.load('preds.npy')

    # save maps as images
    res_img = np.zeros((im_sz * 2, im_sz * 3, 3))

    res_img[im_sz * 0:im_sz * 1, im_sz * 0:im_sz * 1] = preds[0, :, :, 0:3]  # a_to_b add map
    res_img[im_sz * 0:im_sz * 1, im_sz * 1:im_sz * 2] = preds[0, :, :, 3:6]  # a_to_b mult map
    res_img[im_sz * 0:im_sz * 1, im_sz * 2:im_sz * 3, :2] = preds[0, :, :, 6:8]  # a_to_b warp map

    res_img[im_sz * 1:im_sz * 2, im_sz * 0:im_sz * 1] = preds[0, :, :, 8:11]  # b_to_a add map
    res_img[im_sz * 1:im_sz * 2, im_sz * 1:im_sz * 2] = preds[0, :, :, 11:14]  # b_to_a mult map
    res_img[im_sz * 1:im_sz * 2, im_sz * 2:im_sz * 3, :2] = preds[0, :, :, 14:16]  # b_to_a warp map

    res_img = np.clip(res_img, -1, 1)
    res_img = ((res_img + 1) * 127.5).astype(np.uint8)
    cv2.imwrite(os.path.join(output_folder, "maps.jpg"), cv2.cvtColor(res_img, cv2.COLOR_RGB2BGR))

    # apply maps and save results

    org_strength = tf.reshape(tf.range(STEPS, dtype=tf.float32), [STEPS, 1, 1, 1]) / (STEPS - 1)
    trg_strength = tf.reverse(org_strength, axis=[0])

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    video = cv2.VideoWriter(os.path.join(output_folder, "morph.mp4"), fourcc, fps, (im_sz, im_sz))

    img_a = np.zeros((im_sz, im_sz * (STEPS // 10), 3), dtype=np.uint8)
    img_b = np.zeros((im_sz, im_sz * (STEPS // 10), 3), dtype=np.uint8)
    img_a_b = np.zeros((im_sz, im_sz * (STEPS // 10), 3), dtype=np.uint8)

    res_img = np.zeros((im_sz * 3, im_sz * (STEPS // 10), 3), dtype=np.uint8)

    all_im_path = os.path.join(output_folder, "all_steps")
    if not os.path.exists(all_im_path):
        os.mkdir(all_im_path)

    for i in tqdm(range(STEPS), desc="Generating images:"):
        preds_org = preds * org_strength[i]
        preds_trg = preds * trg_strength[i]

        res_targets, res_origins = warp(origins, targets, preds_org[..., :8], preds_trg[..., 8:])
        res_targets = tf.clip_by_value(res_targets, -1, 1)
        res_origins = tf.clip_by_value(res_origins, -1, 1)

        results = res_targets * trg_strength[i] + res_origins * org_strength[i]
        res_numpy = results.numpy()

        img = ((res_numpy[0] + 1) * 127.5).astype(np.uint8)
        video.write(cv2.cvtColor(img, cv2.COLOR_RGB2BGR))

        cv2.imwrite(os.path.join(all_im_path, f"step_{i:05d}.png"), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))

        if (i + 1) % 10 == 0:
            res_img[im_sz * 0:im_sz * 1, i // 10 * im_sz: (i // 10 + 1) * im_sz] = img
            res_img[im_sz * 1:im_sz * 2, i // 10 * im_sz: (i // 10 + 1) * im_sz] = (
                        (res_targets.numpy()[0] + 1) * 127.5).astype(np.uint8)
            res_img[im_sz * 2:im_sz * 3, i // 10 * im_sz: (i // 10 + 1) * im_sz] = (
                        (res_origins.numpy()[0] + 1) * 127.5).astype(np.uint8)

    cv2.imwrite(os.path.join(output_folder, "result.jpg"), cv2.cvtColor(res_img, cv2.COLOR_RGB2BGR))

    cv2.destroyAllWindows()
    video.release()
    print('Result video saved.')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--source", help="Source file name", default=None)
    parser.add_argument("-t", "--target", help="Target file name", default=None)
    parser.add_argument("-e", "--train_epochs", help="Number of epochs to train network", default=TRAIN_EPOCHS,
                        type=int)
    parser.add_argument("-a", "--add_scale", help="Scaler for addition map", default=add_scale, type=float)
    parser.add_argument("-m", "--mult_scale", help="Scaler for multiplication map", default=mult_scale, type=float)
    parser.add_argument("-w", "--warp_scale", help="Scaler for warping map", default=warp_scale, type=float)
    parser.add_argument("-add_first", "--add_first", help="Should you add or multiply maps first", default=add_first,
                        type=bool)

    parser.add_argument("--im_sz", help="Image size for use in morhping. Images will be resized to im_sz x im_sz",
                        default=im_sz, type=int)
    parser.add_argument("--mp_sz", help="Mapping size, the size of the feature map.",
                        default=mp_sz, type=int)

    parser.add_argument("--fps", help="FPS of the result video", default=45, type=int)
    parser.add_argument("--steps", help="Total number of frames to generate", default=100, type=int)
    parser.add_argument("--sleep", help="Sleep for a random number of seconds", default=True, type=bool)

    args = parser.parse_args()

    print("Working directory:", os.getcwd())
    print("File placement:", pathlib.Path(__file__).parent.resolve())
    if not os.getcwd() == pathlib.Path(__file__).parent.resolve():
        os.chdir(pathlib.Path(__file__).parent.resolve())
        print("New working directory:", os.getcwd())

    if not args.source:
        print("No source file provided!")
        exit()
    else:
        print("Source:", args.source)

    if not args.target:
        print("No target file provided!")
        exit()
    else:
        print("Target:", args.target)

    if args.sleep:
        sleep_time = random.uniform(0, 10)
        print(f"Sleeping for {sleep_time} sec")
        time.sleep(sleep_time)

    TRAIN_EPOCHS = args.train_epochs
    add_scale = args.add_scale
    mult_scale = args.mult_scale
    warp_scale = args.warp_scale
    add_first = args.add_first
    mp_sz = args.mp_sz
    im_sz = args.im_sz

    main_folder = "morph/morphed_new" + f"_e{TRAIN_EPOCHS}_af{add_first}_w{warp_scale}_im{im_sz}_mp{mp_sz}"
    i = 0
    output_folder = main_folder + f"_{i}"
    while os.path.exists(output_folder):
        i += 1
        output_folder = main_folder + f"_{i}"
    os.mkdir(output_folder)

    with open(os.path.join(output_folder, "files.txt"), "w") as f:
        for a in args._get_args():
            f.write(f"{a}\n")
        for kw, val in args._get_kwargs():
            f.write(f"{kw} = {val}\n")
        f.write(f"im_sz = {im_sz}\n")
        f.write(f"mp_sz = {mp_sz}\n")

    dom_a = cv2.imread(args.source, cv2.IMREAD_COLOR)
    dom_a = cv2.cvtColor(dom_a, cv2.COLOR_BGR2RGB)
    dom_a = cv2.resize(dom_a, (im_sz, im_sz), interpolation=cv2.INTER_AREA)
    dom_a = dom_a / 127.5 - 1

    dom_b = cv2.imread(args.target, cv2.IMREAD_COLOR)
    dom_b = cv2.cvtColor(dom_b, cv2.COLOR_BGR2RGB)
    dom_b = cv2.resize(dom_b, (im_sz, im_sz), interpolation=cv2.INTER_AREA)
    dom_b = dom_b / 127.5 - 1

    origins = dom_a.reshape(1, im_sz, im_sz, 3).astype(np.float32)
    targets = dom_b.reshape(1, im_sz, im_sz, 3).astype(np.float32)

    produce_warp_maps(origins, targets, output_folder=output_folder)
    use_warp_maps(origins, targets, args.fps, args.steps, output_folder=output_folder)
