// Fixed-size transferable batches. The graph's output is silence: never monitor
// the local microphone back into the speakers. Resampling uses the real context rate.
class V2Microphone extends AudioWorkletProcessor {
  constructor() { super(); this.batch = new Float32Array(512); this.used = 0; }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let sample = 0;
      for (const channel of channels) sample += channel[i];
      this.batch[this.used++] = sample / channels.length;
      if (this.used === this.batch.length) {
        this.port.postMessage(this.batch, [this.batch.buffer]);
        this.batch = new Float32Array(512); this.used = 0;
      }
    }
    return true;
  }
}
registerProcessor('v2-microphone', V2Microphone);
