package io.github.lvhtony.mollyremote;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(MpdPlugin.class);
        registerPlugin(UpdatePlugin.class);
        super.onCreate(savedInstanceState);
        // Ignore Android's font-size setting inside the app so layouts don't overflow (Fold cover screen, S25 Ultra)
        bridge.getWebView().getSettings().setTextZoom(100);
    }
}
