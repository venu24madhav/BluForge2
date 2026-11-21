import javax.swing.*;
import javax.swing.border.*;
import javax.swing.plaf.*;
import javax.swing.table.*;
import java.awt.*;
import java.awt.image.BufferedImage;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.List;
import java.util.jar.*;
import javax.imageio.ImageIO;

/**
 * Headless reference render of the LANforge GUI look (Metal/Ocean + lfclient UIDefaults overrides).
 * READ-ONLY w.r.t. the system: never opens a display, never connects to the real GUI.
 * Overrides copied from candela.lanforge.lfclient bytecode (javap), fonts read straight from lfclient.jar.
 */
public class RefRender {
    static String hex(Color c) { return String.format("#%02x%02x%02x", c.getRed(), c.getGreen(), c.getBlue()); }

    public static void main(String[] args) throws Exception {
        System.setProperty("java.awt.headless", "true");
        System.setProperty("swing.aatext", "true");
        System.setProperty("awt.useSystemAAFontSettings", "lcd");
        String outDir = args.length > 0 ? args[0] : ".";
        String jar = "/home/lanforge/LANforgeGUI_5.5.3/lfclient.jar";
        PrintWriter log = new PrintWriter(new FileWriter(outDir + "/ui_defaults_dump.txt"));

        // 1. Metal L&F (SYSTEM_LOOK_FEEL=false in lfcnf.txt)
        UIManager.setLookAndFeel(UIManager.getCrossPlatformLookAndFeelClassName());
        log.println("# LookAndFeel: " + UIManager.getLookAndFeel().getName() + " / " + UIManager.getLookAndFeel().getClass().getName());
        log.println("# java.version=" + System.getProperty("java.version"));

        // 2. lfclient.java overrides (from bytecode)
        UIDefaults d = UIManager.getLookAndFeelDefaults();
        Color selBg = new Color(180, 220, 255), selFg = new Color(0, 5, 0);
        String[] selKeys = {"CheckBoxMenuItem", "ComboBox", "EditorPane", "FormattedTextField", "List", "MenuItem", "PasswordField",
                "PopupMenu", "RadioButtonMenuItem", "Table", "TextArea", "TextField", "TextPane", "Tree"};
        d.put("Table.gridColor", new Color(190, 190, 200));
        for (String k : selKeys) { d.put(k + ".selectionBackground", selBg); d.put(k + ".selectionForeground", selFg); }
        d.put("RadioButton.select", selBg);
        d.put("Table.disabled", Boolean.FALSE);
        d.put("Table.showGrid", Boolean.TRUE);
        d.put("Table.intercellSpacing", new Dimension(1, 1));
        d.put("TextArea.font", new Font("Arial", Font.PLAIN, 9));
        d.put("TextPane.font", new Font("Courier", Font.PLAIN, 11));
        d.put("EditorPane.font", new Font("Courier", Font.PLAIN, 11));
        UIManager.put("swing.boldMetal", Boolean.FALSE);

        // 3. Fonts from the jar (stream only; nothing extracted to disk)
        GraphicsEnvironment ge = GraphicsEnvironment.getLocalGraphicsEnvironment();
        Map<String, Font> fontMap = new TreeMap<>();
        try (JarFile jf = new JarFile(jar)) {
            for (String f : new String[]{"century-gothic-sans-bold.ttf", "century-gothic-sans.ttf", "LucidaBrightDemiBold.ttf",
                    "LucidaBrightDemiItalic.ttf", "LucidaBrightItalic.ttf", "LucidaBrightRegular.ttf", "LucidaSansDemiBold.ttf",
                    "LucidaSansDemiOblique.ttf", "LucidaSansOblique.ttf", "LucidaSansRegular.ttf", "LucidaTypewriterBoldOblique.ttf",
                    "LucidaTypewriterBold.ttf", "LucidaTypewriterOblique.ttf", "LucidaTypewriterRegular.ttf"}) {
                JarEntry e = jf.getJarEntry("fonts/" + f);
                if (e == null) { log.println("# MISSING font " + f); continue; }
                try (InputStream in = jf.getInputStream(e)) {
                    Font fo = Font.createFont(Font.TRUETYPE_FONT, in);
                    ge.registerFont(fo);
                    fontMap.put(fo.getName(), fo);
                    log.println("# Loaded font file=" + f + " getName=" + fo.getName() + " family=" + fo.getFamily() + " fontName=" + fo.getFontName());
                }
            }
        }
        Font lbr = fontMap.get("LucidaBrightRegular");
        log.println("# font_map has LucidaBrightRegular: " + (lbr != null));
        Font probe = new Font("LucidaBrightRegular", Font.PLAIN, 12);
        log.println("# new Font(\"LucidaBrightRegular\",PLAIN,12) -> family=" + probe.getFamily() + " fontName=" + probe.getFontName());
        if (lbr != null) d.put("defaultFont", lbr.deriveFont(Font.PLAIN, 12f));
        FontUIResource fur = new FontUIResource("LucidaBrightRegular", -2, 12);
        log.println("# FontUIResource(LucidaBrightRegular,-2,12) -> family=" + fur.getFamily() + " style=" + fur.getStyle());
        for (Enumeration<Object> en = UIManager.getDefaults().keys(); en.hasMoreElements(); ) {
            Object k = en.nextElement();
            Object v = UIManager.get(k);
            if (v instanceof FontUIResource) UIManager.put(k, fur);
        }

        // 4. Dump resolved colours / fonts / gradients
        TreeMap<String, String> dump = new TreeMap<>();
        UIDefaults all = UIManager.getDefaults();
        for (Enumeration<Object> en = all.keys(); en.hasMoreElements(); ) {
            Object k = en.nextElement();
            Object v = UIManager.get(k);
            if (v instanceof Color) dump.put(k.toString(), hex((Color) v));
            else if (v instanceof Font) dump.put(k.toString(), "FONT " + ((Font) v).getFamily() + " " + ((Font) v).getStyle() + " " + ((Font) v).getSize());
            else if (v instanceof List && k.toString().endsWith("gradient")) {
                StringBuilder sb = new StringBuilder("GRADIENT");
                for (Object o : (List<?>) v) sb.append(' ').append(o instanceof Color ? hex((Color) o) : String.valueOf(o));
                dump.put(k.toString(), sb.toString());
            } else if (v instanceof Insets) dump.put(k.toString(), "INSETS " + v);
        }
        for (Map.Entry<String, String> e : dump.entrySet()) log.println(e.getKey() + " = " + e.getValue());
        log.close();

        // 5. Render Port Mgr screen
        BufferedImage img = renderPortMgr(1680, 997, outDir + "/ports.tsv", lbr);
        ImageIO.write(img, "png", new File(outDir + "/../screens/synth_lanforge_portmgr_1680x997.png"));
        System.out.println("wrote synth_lanforge_portmgr_1680x997.png");
    }

    static void layoutAll(Container c) {
        c.doLayout();
        for (Component ch : c.getComponents()) if (ch instanceof Container) layoutAll((Container) ch);
    }

    static JPanel flow(int align, int hgap, int vgap) { return new JPanel(new FlowLayout(align, hgap, vgap)); }

    static BufferedImage renderPortMgr(int W, int H, String tsv, Font lbr) throws Exception {
        Font tblFont = new Font("Dialog", Font.PLAIN, 12);   // tbl_column_font / tbl_cll_font in LANforgeMgr
        JPanel root = new JPanel(new BorderLayout());

        // menu bar (labels from MessageBundle_en_US: Control / Reporting / Windows / Info / Tests)
        JMenuBar mb = new JMenuBar();
        for (String m : new String[]{"Control", "Reporting", "Windows", "Info", "Tests"}) mb.add(new JMenu(m));

        // top button row
        JPanel top = new JPanel(new BorderLayout());
        JPanel btns = flow(FlowLayout.RIGHT, 12, 4);
        for (String b : new String[]{"Stop All", "Restart Manager", "Refresh", "HELP"}) btns.add(new JButton(b));
        top.add(mb, BorderLayout.NORTH);
        top.add(btns, BorderLayout.CENTER);

        // tab pane (wrapped, tabs visible per lfcnf.txt NO_* flags)
        JTabbedPane tabs = new JTabbedPane(JTabbedPane.TOP, JTabbedPane.WRAP_TAB_LAYOUT);
        String[] names = {"Status", "Layer-3", "L3 Endps", "Armageddon", "WanLinks", "Attenuators", "RF-Generator", "File-IO", "Layer-4",
                "Resource Mgr", "Alerts", "Port Mgr", "vAP Stations", "Messages", "Warnings", "Wifi-Messages", "DUT", "Profiles", "Traffic-Profiles", "Interop", "Cell Emulator"};
        int sel = 0;
        for (int i = 0; i < names.length; i++) {
            JPanel p = new JPanel(new BorderLayout());
            if (names[i].equals("Port Mgr")) { sel = i; p = portPanel(tsv, tblFont); }
            tabs.addTab(names[i], p);
        }
        tabs.setSelectedIndex(sel);

        JLabel status = new JLabel("Logged in to: localhost:4002   as: Admin");
        status.setBorder(BorderFactory.createCompoundBorder(BorderFactory.createEtchedBorder(), new EmptyBorder(2, 6, 2, 6)));

        root.add(top, BorderLayout.NORTH);
        root.add(tabs, BorderLayout.CENTER);
        root.add(status, BorderLayout.SOUTH);
        root.setBorder(new EmptyBorder(0, 0, 0, 0));
        root.addNotify();
        root.setSize(W, H);
        layoutAll(root); layoutAll(root);
        BufferedImage img = new BufferedImage(W, H, BufferedImage.TYPE_INT_RGB);
        Graphics2D g = img.createGraphics();
        g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_LCD_HRGB);
        RepaintManager.currentManager(root).setDoubleBufferingEnabled(false);
        root.paint(g);
        g.dispose();
        return img;
    }

    static JPanel portPanel(String tsv, Font tblFont) throws Exception {
        JPanel p = new JPanel(new BorderLayout());
        // control rows (mirrors docs gui_ports.png)
        JPanel ctl = new JPanel(new GridLayout(2, 1, 0, 2));
        JPanel r1 = flow(FlowLayout.LEFT, 8, 3);
        r1.add(new JLabel("Disp:")); JTextField disp = new JTextField("1.1.4:0", 14); r1.add(disp);
        r1.add(new JButton("Sniff Packets"));
        r1.add(new JCheckBox("Down", true));
        r1.add(new JButton("Clear Counters")); r1.add(new JButton("Reset Port")); r1.add(new JButton("Delete"));
        JPanel r2 = flow(FlowLayout.LEFT, 8, 3);
        r2.add(new JLabel("Rpt Timer:")); r2.add(new JComboBox<>(new String[]{"medium  (8 s)", "fast  (1 s)", "slow  (30 s)"}));
        r2.add(new JButton("Apply")); r2.add(new JCheckBox("VRF"));
        r2.add(new JButton("Display")); r2.add(new JButton("Create")); r2.add(new JButton("Modify")); r2.add(new JButton("Batch Modify"));
        ctl.add(r1); ctl.add(r2);
        p.add(ctl, BorderLayout.NORTH);

        String[] cols = {"Port", "Phantom", "Down", "IP", "SEC", "Alias", "Parent Dev", "RX Bytes", "RX Pkts", "Pps RX", "bps RX", "TX Bytes", "TX Pkts", "Pps TX", "bps TX", "MAC", "Port Type", "Mode"};
        List<String> lines = Files.readAllLines(Paths.get(tsv));
        DefaultTableModel m = new DefaultTableModel(cols, 0) {
            public Class<?> getColumnClass(int c) { return (c == 1 || c == 2) ? Boolean.class : String.class; }
            public boolean isCellEditable(int r, int c) { return false; }
        };
        java.text.DecimalFormat f = new java.text.DecimalFormat("#,###");
        for (String l : lines) {
            String[] t = l.split("\t", -1);
            if (t.length < 17) continue;
            m.addRow(new Object[]{t[0].replace("1.1.0", "1.1.00").replace("1.1.000", "1.1.00"), Boolean.valueOf(t[4]), Boolean.valueOf(t[3]), t[2], "0", t[1], t[13],
                    f.format(Long.parseLong(t[5])), f.format(Long.parseLong(t[6])), t[7], f.format(Long.parseLong(t[8])),
                    f.format(Long.parseLong(t[9])), f.format(Long.parseLong(t[10])), t[11], f.format(Long.parseLong(t[12])), t[14], t[15], t[16]});
        }
        JTable tbl = new JTable(m);
        tbl.setFont(tblFont);
        tbl.getTableHeader().setFont(tblFont);
        tbl.setRowHeight(19);
        tbl.setAutoResizeMode(JTable.AUTO_RESIZE_OFF);
        int[] w = {56, 60, 46, 104, 36, 70, 74, 120, 90, 56, 90, 120, 90, 56, 90, 130, 90, 100};
        for (int i = 0; i < w.length; i++) tbl.getColumnModel().getColumn(i).setPreferredWidth(w[i]);
        DefaultTableCellRenderer right = new DefaultTableCellRenderer(); right.setHorizontalAlignment(SwingConstants.RIGHT);
        for (int i : new int[]{4, 7, 8, 9, 10, 11, 12, 13, 14}) tbl.getColumnModel().getColumn(i).setCellRenderer(right);
        if (m.getRowCount() > 1) tbl.setRowSelectionInterval(1, 1);
        System.out.println("DEBUG rows=" + m.getRowCount() + " selRow=" + tbl.getSelectedRow() + " selBg=" + hex(tbl.getSelectionBackground()) + " selFg=" + hex(tbl.getSelectionForeground()) + " grid=" + hex(tbl.getGridColor()) + " showGrid=" + tbl.getShowHorizontalLines());
        JScrollPane sp = new JScrollPane(tbl, ScrollPaneConstants.VERTICAL_SCROLLBAR_ALWAYS, ScrollPaneConstants.HORIZONTAL_SCROLLBAR_ALWAYS);
        sp.setBorder(BorderFactory.createTitledBorder("All Ethernet Interfaces (Ports) for all Resources."));
        ((TitledBorder) sp.getBorder()).setTitleJustification(TitledBorder.CENTER);
        p.add(sp, BorderLayout.CENTER);
        return p;
    }
}
