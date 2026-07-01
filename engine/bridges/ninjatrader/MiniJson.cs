// MiniJson.cs — minimal flat-object JSON reader for EngineRelay (NT8 has no
// bundled JSON). Handles the engine's flat order messages (string / number /
// null / bool values, no nesting). Compile alongside EngineRelay.cs.
using System.Collections.Generic;
using System.Globalization;

namespace NinjaTrader.NinjaScript.Strategies
{
    public class MiniJson
    {
        private readonly Dictionary<string, string> kv = new Dictionary<string, string>();

        public static MiniJson Parse(string s)
        {
            var j = new MiniJson();
            s = s.Trim();
            if (s.StartsWith("{")) s = s.Substring(1);
            if (s.EndsWith("}")) s = s.Substring(0, s.Length - 1);
            int i = 0, n = s.Length;
            while (i < n)
            {
                while (i < n && s[i] != '"') i++;          // find key open-quote
                if (i >= n) break;
                i++; int ks = i; while (i < n && s[i] != '"') i++;
                string key = s.Substring(ks, i - ks); i++;
                while (i < n && s[i] != ':') i++; i++;
                while (i < n && char.IsWhiteSpace(s[i])) i++;
                string val;
                if (i < n && s[i] == '"')                   // quoted string
                {
                    i++; int vs = i; while (i < n && s[i] != '"') i++;
                    val = s.Substring(vs, i - vs); i++;
                }
                else                                        // number / null / bool
                {
                    int vs = i; while (i < n && s[i] != ',') i++;
                    val = s.Substring(vs, i - vs).Trim();
                }
                kv[key] = val;
                while (i < n && s[i] != ',') i++; i++;
            }
            return j;
        }

        public string Get(string k) { return kv.TryGetValue(k, out var v) ? v : ""; }

        public double Num(string k)
        {
            return kv.TryGetValue(k, out var v) &&
                   double.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out var d) ? d : 0.0;
        }
    }
}
