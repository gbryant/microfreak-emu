//
// A logging I2C slave, used to find out what the MicroFreak's main firmware
// expects from the device at 7-bit address 0x18.
//
// This deliberately acknowledges everything and answers reads with a
// configurable byte, so the firmware gets past its address phase and we can
// watch the register sequence it writes.  Identifying the real part from that
// sequence is more useful than a teardown photo would be: what has to be
// modelled is the behaviour the firmware checks, not the part number.
//
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.I2C
{
    public class MicroFreakCodec : II2CPeripheral
    {
        public MicroFreakCodec()
        {
            written = new List<byte>();
            registers = new Dictionary<byte, byte>();
        }

        // What an unwritten register reads back as.  0xFF and 0x00 are both
        // worth trying: a firmware that checks a device ID will reject one of
        // them, and which one it rejects is itself informative.
        public byte DefaultValue { get; set; }

        public void Write(byte[] data)
        {
            this.Log(LogLevel.Error, "I2C WRITE {0}",
                     string.Join(" ", data.Select(b => b.ToString("X2"))));
            // Convention for almost every codec: first byte is the register
            // index, the rest are values written from there on.
            if(data.Length >= 1)
            {
                lastRegister = data[0];
                for(var i = 1; i < data.Length; i++)
                {
                    registers[(byte)(lastRegister + i - 1)] = data[i];
                }
            }
            written.AddRange(data);
        }

        public byte[] Read(int count = 1)
        {
            var outv = new byte[count];
            for(var i = 0; i < count; i++)
            {
                byte v;
                if(!registers.TryGetValue((byte)(lastRegister + i), out v))
                {
                    v = DefaultValue;
                }
                outv[i] = v;
            }
            this.Log(LogLevel.Error, "I2C READ reg 0x{0:X2} -> {1}", lastRegister,
                     string.Join(" ", outv.Select(b => b.ToString("X2"))));
            return outv;
        }

        public void FinishTransmission()
        {
            written.Clear();
        }

        public void Reset()
        {
            written.Clear();
            registers.Clear();
            lastRegister = 0;
        }

        private byte lastRegister;
        private readonly List<byte> written;
        private readonly Dictionary<byte, byte> registers;
    }
}
